# Browser dependencies

Locally bundled files; the browser does not contact a CDN at runtime.

- marked 18.0.13: Markdown rendering. Source: https://cdn.jsdelivr.net/npm/marked@18.0.13/lib/marked.umd.js ; license in marked.LICENSE.
- DOMPurify 3.4.15: sanitizes rendered Markdown. Source: https://cdn.jsdelivr.net/npm/dompurify@3.4.15/dist/purify.min.js ; license in dompurify.LICENSE.

Versions checked against the npm registry on 2026-09-19. Model output is sanitized with an explicit tag/attribute allowlist; images, forms and raw scripts are not allowed.
