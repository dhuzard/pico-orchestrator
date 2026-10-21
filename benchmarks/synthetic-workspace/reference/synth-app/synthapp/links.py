"""Link helpers of synth-app."""


def post_url(title, slugify):
    slug = slugify(title)
    if not slug:
        raise ValueError("title has no usable characters")
    return f"/posts/{slug}"
