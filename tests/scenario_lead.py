"""A scripted stand-in for the lead model of one child run, for the synthetic pilot (issue #49).

`FAKE_PI` in tests/test_headless_run.py is a fake `pi`: it takes the run lease, creates or restores
the parent run through tools/parent_orchestration.py and then acts out FAKE_PI_BEHAVIOR. The behavior
`scenario` hands over to `run()` below, which replays the ops a scenario file gives for this task and
this launch. No model is called: every ops list is data written by the test.

The ops are what the lifecycle extension and the lead would do, kept to the effects that matter:

    {"op": "event", "event": {...}}          one recorder call through the real CLI, with the lease owner
    {"op": "work"}                           apply the benchmark's reference solution on branch pico/<task>
    {"op": "finish", "status": "PARTIAL", "blocker": "CODE"}
    {"op": "barrier", "name": "wave", "parties": 2}   wait until that many children reached it
    {"op": "wait_file", "name": "go"}        wait for the test to create a signal file
    {"op": "lead_limit"} / {"op": "lead_calls", "n": 4} / {"op": "lead_stop"}   the lead-call limit (#47)
    {"op": "hang"} / {"op": "crash"}         never return / exit non-zero without recording an outcome

Inside an event, "$TASK", "$HEAD" (the checkout's HEAD), "$INPUTS" (the launcher's inputs digest) and
"$BUDGET" (the failure retry budget the launcher handed over, else the default 2) are substituted.
A recorder call that is refused is a bug in the scenario unless the op says `"expect": "error"`: the
child then exits 9 and writes `fake-scenario-error` to its log, which the tests look for.
A short form of every recorder answer (the retry gate decision, else ok or the error code) is appended to
`events-<task>.jsonl` in the execution's session directory.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time

ERROR_MARKER = "fake-scenario-error"
SIGNAL_TIMEOUT_SECONDS = 30


def die(message: str, code: int = 9):
    print(json.dumps({"type": ERROR_MARKER, "message": message}))
    sys.stdout.flush()
    sys.exit(code)


class Lead:
    def __init__(self, *, tools: str, run_id: str, owner: str, session_dir: str):
        self.tools, self.run_id, self.owner = tools, run_id, owner
        self.task = os.environ["PI_ORCHESTRATION_EXECUTION_ID"].split("--", 1)[1]
        self.session_dir = session_dir
        self.cli = os.path.join(tools, "parent_orchestration.py")
        self.calls = 0
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    # -- recorder -------------------------------------------------------------------------------------
    def substitute(self, value):
        if isinstance(value, dict):
            return {key: self.substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.substitute(item) for item in value]
        table = {"$TASK": self.task, "$HEAD": self.head, "$INPUTS": os.environ.get("PI_ORCHESTRATION_INPUTS_DIGEST"),
                 "$BUDGET": int(os.environ.get("PI_ORCHESTRATION_FAILURE_RETRY_BUDGET", "2"))}
        if isinstance(value, str):
            return table[value] if value in table else value.replace("$TASK", self.task)
        return value

    def answer(self, command: list[str], payload: dict | None = None) -> dict:
        done = subprocess.run([sys.executable, *command], input=None if payload is None else json.dumps(payload),
                              capture_output=True, text=True)
        try:
            result = json.loads(done.stdout)
        except ValueError:
            die(f"unreadable recorder answer for {command[-3:]}: {done.stdout[:200]} {done.stderr[:200]}")
        brief = result.get("retry_gate") or {"ok": result.get("ok", True), "error_code": result.get("error_code")}
        with open(os.path.join(self.session_dir, f"events-{self.task}.jsonl"), "a") as log:
            log.write(json.dumps({"request": payload, "answer": brief}, sort_keys=True) + "\n")
        return result

    def event(self, event: dict, expect: str = "ok") -> dict:
        result = self.answer([self.cli, "event", self.run_id, "--owner", self.owner], self.substitute(event))
        failed = result.get("ok") is False
        if failed != (expect == "error"):
            die(f"recorder {'refused' if failed else 'accepted'} {event.get('kind')}: {json.dumps(result)[:300]}")
        return result

    # -- ops ------------------------------------------------------------------------------------------
    def op_event(self, op):
        self.event(op["event"], op.get("expect", "ok"))

    def op_finish(self, op):
        command = [self.cli, "finish", self.run_id, op["status"], "--owner", self.owner]
        if op.get("blocker"):
            command += ["--blocker-code", op["blocker"]]
        result = self.answer(command)
        if result.get("ok") is False:
            die(f"finish {op['status']} refused: {json.dumps(result)[:300]}")

    def op_work(self, op):
        """The worker's result: a worktree on pico/<task> holding the reference solution, committed."""
        repo = os.path.basename(os.getcwd())
        source = os.path.join(os.environ["FAKE_SCENARIO_REFERENCE"], repo)
        branch = f"pico/{self.task}"
        tree = os.path.join(self.session_dir, "..", f"wt-{self.task}")
        known = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], capture_output=True)
        add = ["git", "worktree", "add", "-q", tree, branch] if known.returncode == 0 else \
              ["git", "worktree", "add", "-q", "-b", branch, tree]
        if subprocess.run(add, capture_output=True, text=True).returncode:
            die("cannot create the worker worktree")
        shutil.copytree(source, tree, dirs_exist_ok=True)
        identity = ["-c", "user.name=Scenario Worker", "-c", "user.email=worker@example.invalid"]
        for command in (["add", "-A"], [*identity, "commit", "-q", "-m", f"Reference solution for {self.task}"]):
            if subprocess.run(["git", "-C", tree, *command], capture_output=True, text=True).returncode:
                die("cannot commit the reference solution")

    def signal_path(self, name: str) -> str:
        return os.path.join(os.environ["FAKE_SCENARIO_SIGNALS"], name)

    def wait_until(self, condition, what: str):
        deadline = time.monotonic() + SIGNAL_TIMEOUT_SECONDS
        while not condition():
            if time.monotonic() > deadline:
                die(f"timed out waiting for {what}")
            time.sleep(0.02)

    def op_barrier(self, op):
        """Prove the children overlap: nobody continues before `parties` children arrived."""
        open(self.signal_path(f"{op['name']}.{self.task}"), "w").close()
        self.wait_until(lambda: len(glob.glob(self.signal_path(f"{op['name']}.*"))) >= op["parties"], f"barrier {op['name']}")

    def op_wait_file(self, op):
        self.wait_until(lambda: os.path.exists(self.signal_path(op["name"])), f"signal {op['name']}")

    def op_signal(self, op):
        open(self.signal_path(op["name"]), "w").close()

    def op_lead_limit(self, op):
        """What the extension records at start: the limit the launcher handed over."""
        event = {"kind": "lead_limit", "state": "set", "max_lead_calls": int(os.environ["PI_ORCHESTRATION_MAX_LEAD_CALLS"]),
                 "source": os.environ.get("PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE", "env")}
        if os.environ.get("PI_ORCHESTRATION_LEAD_CALLS_WARN_AT"):
            event["warn_at"] = int(os.environ["PI_ORCHESTRATION_LEAD_CALLS_WARN_AT"])
        self.event(event)

    def op_lead_calls(self, op):
        maximum = int(os.environ.get("PI_ORCHESTRATION_MAX_LEAD_CALLS", "0"))
        warn_at = int(os.environ.get("PI_ORCHESTRATION_LEAD_CALLS_WARN_AT", "0"))
        for _ in range(op["n"]):
            self.calls += 1
            self.event({"kind": "lead_usage", "model": "openai-codex/gpt-6.1-sol"})
            if warn_at and self.calls == warn_at:
                self.event({"kind": "lead_call_warning", "calls": self.calls})
            if maximum and self.calls == maximum:
                self.event({"kind": "lead_limit", "state": "reached", "calls": self.calls})

    def op_lead_stop(self, op):
        """The extension's resumable stop at the limit: record it, release the lease, end without an outcome."""
        self.event({"kind": "lead_call_limit_reached", "calls": self.calls})
        self.answer([os.path.join(self.tools, "run_lease.py"), "release", self.run_id, "--owner", self.owner])
        sys.exit(0)

    def op_hang(self, op):
        time.sleep(120)  # the test stops this process; the bound only protects against a forgotten child

    def op_crash(self, op):
        sys.exit(3)


def run(*, tools: str, run_id: str, owner: str, session_dir: str):
    lead = Lead(tools=tools, run_id=run_id, owner=owner, session_dir=session_dir)
    with open(os.environ["FAKE_SCENARIO"], encoding="utf-8") as handle:
        launches = json.load(handle).get(lead.task)
    if launches is None:
        die(f"the scenario has no script for task {lead.task}")
    counter = os.path.join(session_dir, f"launches-{lead.task}")
    index = len(open(counter).read().splitlines()) if os.path.exists(counter) else 0
    with open(counter, "a") as handle:
        handle.write("launch\n")
    if index >= len(launches):
        die(f"the scenario has no script for launch {index + 1} of task {lead.task}")
    for op in launches[index]:
        handler = getattr(lead, "op_" + op["op"], None)
        if handler is None:
            die(f"unknown scenario op {op['op']}")
        handler(op)
