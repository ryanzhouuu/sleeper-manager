"""Small inline icons keep the owner controls independent of asset requests."""

_PATHS = {
    "wait": '<circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/>',
    "lock": '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "pass": '<path d="m5 5 10 7-10 7V5Zm14 0v14"/>',
    "arrow": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    "alert": '<path d="m12 3 10 18H2L12 3Zm0 6v4m0 3v1"/>',
}


def icon(name: str) -> str:
    return (
        '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true" focusable="false">{_PATHS[name]}</svg>'
    )
