"""Sphinx configuration of the Chinese documentation site.

An English site, when added, lives in docs/en/ with its own conf.py that imports this file and changes only `language` and `exclude_patterns`.
"""
from docutils.transforms import Transform

project = 'claude-sessiond'
author = 'Ayaka Mikazuki'
copyright = '2026, Ayaka Mikazuki'
language = 'zh_CN'
extensions = ['myst_parser']
html_theme = 'sphinx_book_theme'
html_title = 'claude-sessiond'
html_theme_options = {
    'repository_url': 'https://github.com/ayaka14732/claude-sessiond',
    'use_repository_button': True,
}
myst_heading_anchors = 3
# The Chinese site must not pick up the English pages under docs/en/.
exclude_patterns = ['_build', 'en']

class HeadingSlugIds(Transform):
    """Replace the HTML id of each heading with the slug MyST generates by GitHub's rules, so that anchors follow the heading text, Chinese characters included.

    docutils drops non-ASCII characters when it generates ids, which leaves Chinese headings with ids such as `id1`.
    """
    # After docutils PropagateTargets (260); before MyST ResolveAnchorIds and StandardDomain.process_doc (879), so that links, the table of contents and permalinks all see the new ids.
    default_priority = 500

    def apply(self) -> None:
        document = self.document
        # The same dict is stored in env.metadata, where MyST resolves [text](page.md#slug) links between pages.
        slugs = getattr(document, 'myst_slugs', {})
        for slug, (line, old, title) in list(slugs.items()):
            if slug == old:
                continue
            if slug in document.ids:
                document.reporter.warning(f'heading slug {slug!r} is already used as an id', line=line)
                continue
            section = document.ids.pop(old)
            section['ids'][section['ids'].index(old)] = slug
            document.ids[slug] = section
            for name, refid in document.nameids.items():
                if refid == old:
                    document.nameids[name] = slug
            slugs[slug] = (line, slug, title)

def setup(app: object) -> None:
    app.add_transform(HeadingSlugIds)
