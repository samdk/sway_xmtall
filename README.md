# sway-xmtall

An xmonad-like auto-tiler for sway. Roughly implements a xmonad's default
"tall" layout: primary column on the left, secondary column on the right.
Effectively a simplified rewrite of
[swaymonad](https://github.com/nicolasavru/swaymonad) with fewer features and
less code.)

(Almost all of the actual rewrite was done with Claude Opus 4.6, based on a mix
of the original swaymonad and a private fork.)

Requires [i3ipc-python](https://github.com/altdesktop/i3ipc-python).


## Usage

```
python3 sway_xmtall.py [--verbose] [--log-file PATH]
```

Add to your sway config to start automatically:

```
exec_always python3 path/to/sway_xmtall.py
```

## Sway config

All commands are sent via sway's `nop` command. Add these bindings to `~/.config/sway/config`:

```
# Focus next/prev window in tiling order
bindsym $mod+j nop focus_next_window
bindsym $mod+k nop focus_prev_window

# Swap focused window with next/prev
bindsym $mod+Shift+j nop swap_with_next_window
bindsym $mod+Shift+k nop swap_with_prev_window

# Promote focused window to the primary (largest) position
bindsym $mod+Return nop promote_window

# Increase/decrease the number of windows in the left column
bindsym $mod+comma nop flow_left
bindsym $mod+period nop flow_right

# Resize the column divider (always moves the divider between columns)
bindsym $mod+h nop move_divider left
bindsym $mod+l nop move_divider right

# Zoom: float window to fill workspace without triggering real fullscreen
# (swaybar stays visible, Chrome keeps tabs, etc.)
bindsym $mod+f nop fullscreen

# Optional but recommended sway bindings
# Real sway fullscreen (bypass sway-xmtall, use sway directly)
bindsym $mod+Shift+f fullscreen
# Resize individual container vertically
bindsym $mod+a resize grow height 20px
bindsym $mod+z resize shrink height 20px
```

## Commands

| Command | Description |
|---|---|
| `focus_next_window` | Focus next window in tiling order |
| `focus_prev_window` | Focus previous window in tiling order |
| `swap_with_next_window` | Swap focused window with next |
| `swap_with_prev_window` | Swap focused window with previous |
| `promote_window` | Swap focused window with the largest window |
| `flow_left` | Move one window from the right -> left column |
| `flow_right` | Move one window from the left -> right column |
| `move_divider left\|right [amount]` | Move the column divider left or right. Always targets the left column so the divider moves consistently regardless of focus. Amount defaults to `50px`. |
| `fullscreen` | Toggle zoom: float window to fill workspace rect without real fullscreen. Press again to restore tiling position. |
