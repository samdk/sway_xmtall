# swaymonad

An xmonad-like auto-tiler for sway. Implements a "tall" layout: primary column on the left, secondary column on the right.

Requires [i3ipc-python](https://github.com/altdesktop/i3ipc-python).

## Usage

```
python3 swaymonad.py [--verbose] [--log-file PATH]
```

Add to your sway config to start automatically:

```
exec python3 /path/to/swaymonad.py
```

## Sway config

All commands are sent via sway's `nop` command. Add these bindings to `~/.config/sway/config`:

```
set $mod Mod4

# Focus next/prev window in tiling order
bindsym $mod+j nop focus_next_window
bindsym $mod+k nop focus_prev_window

# Swap focused window with next/prev
bindsym $mod+Shift+j nop swap_with_next_window
bindsym $mod+Shift+k nop swap_with_prev_window

# Promote focused window to the primary (largest) position
bindsym $mod+Return nop promote_window

# Increase/decrease the number of windows in the left column
bindsym $mod+comma nop increment_lcol
bindsym $mod+period nop decrement_lcol

# Resize the column divider (always moves the divider between columns)
bindsym $mod+h nop resize shrink
bindsym $mod+l nop resize grow

# Zoom: float window to fill workspace without triggering real fullscreen
# (swaybar stays visible, Chrome keeps tabs, etc.)
bindsym $mod+f nop fullscreen

# Real sway fullscreen (bypass swaymonad, use sway directly)
bindsym $mod+Shift+f fullscreen
```

## Commands

| Command | Description |
|---|---|
| `focus_next_window` | Focus next window in tiling order |
| `focus_prev_window` | Focus previous window in tiling order |
| `swap_with_next_window` | Swap focused window with next |
| `swap_with_prev_window` | Swap focused window with previous |
| `promote_window` | Swap focused window with the largest window |
| `increment_lcol` | Add one more window to the left column |
| `decrement_lcol` | Remove one window from the left column |
| `resize grow\|shrink` | Grow/shrink the left column by 50px. Always targets the left column so the divider moves consistently regardless of focus. |
| `fullscreen` | Toggle zoom: float window to fill workspace rect without real fullscreen. Press again to restore tiling position. |
