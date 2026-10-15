"""Application CSS."""


CSS = """
.log-view {
    font-size: 0.85em;
}
.msg-bubble {
    border-radius: 14px;
    padding: 6px 11px;
    background: alpha(@window_fg_color, 0.08);
}
.msg-bubble.own {
    background: @accent_bg_color;
    color: @accent_fg_color;
}
window.dark-ui .pane-sep { background-color: @headerbar_shade_color; }
.msg-author { font-weight: bold; font-size: 0.85em; }
/* Adwaita gives horizontal progress bars a 150px minimum; let width_request win */
progressbar.tx-bar > trough { min-width: 0; }
.msg-mention {
    background: alpha(@accent_bg_color, 0.16);
    border-left: 3px solid @accent_bg_color;
    border-radius: 4px;
    padding: 2px 8px;
}
.msg-meta { font-size: 0.75em; opacity: 0.65; }
.unread-badge {
    background: @accent_bg_color;
    color: @accent_fg_color;
    border-radius: 99px;
    padding: 0 7px;
    min-height: 18px;
    font-size: 0.8em;
    font-weight: bold;
}
"""
