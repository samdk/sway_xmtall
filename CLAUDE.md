# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

sway-xmtall is an xmonad-style auto-tiling window manager for Sway (Wayland), written as a single Python script (`sway_xmtall.py`). It implements a "tall" layout: primary column on the left, secondary on the right. Based on [swaymonad](https://github.com/nicolasavru/swaymonad).

## Running

```bash
python3 sway_xmtall.py [--verbose] [--log-file PATH] [--delay SECONDS]
```

No build step. Only external dependency is [i3ipc-python](https://github.com/altdesktop/i3ipc-python).

## Hooks

A PostToolUse hook runs `python3 -m py_compile` on any `.py` file after Edit/Write to catch syntax errors immediately.

## Architecture

The entire program is in `sway_xmtall.py` (~880 lines). It connects to Sway via i3ipc, listens for window and binding events, and maintains a two-column tiling layout.

### Key abstractions

- **`Layout`**: Per-workspace intent — number of left-column windows (`n_lcol`), saved right-column width, zoom state. Only mutated by user commands, never by sway events.
- **`WorkspaceState`**: Pairs a `Layout` with a tree snapshot and a `pending_moves` counter (for suppressing self-triggered events).
- **`State`**: Global dict of `WorkspaceState` keyed by workspace ID. `get()` creates on demand, inferring layout from the current tree.
- **`SwayConnection`** (extends `i3ipc.Connection`): Adds command batching — multiple sway commands are joined with `;` and sent in one IPC call to reduce latency.

### Core flow

1. **`reflow_workspace()`** — the layout engine. Given a workspace tree node and a `Layout`, computes and issues move commands to enforce the two-column layout (left column gets exactly `n_lcol` windows, rest go right).
2. **Event handlers** (`on_window_new`, `on_window_close`, `on_window_move`) detect changes via tree snapshots and call reflow. `on_window_new` has a fast speculative path that computes moves from the snapshot without refetching.
3. **Command handlers** (dispatched by `on_binding` from sway `nop` bindings) implement user actions: focus/swap/promote, flow_left/right, move_divider, zoom toggle.

### Important patterns

- **Snapshot-based change detection**: Stores last tree state per workspace; diffs against current tree to detect opens/closes.
- **Self-trigger suppression**: `pending_moves` counter distinguishes script-issued moves from user-initiated ones in `on_window_move`.
- **Speculative reflow**: `speculative_swap_and_reflow()` batches swap+reflow commands from the snapshot without an extra IPC round-trip; falls back to sync path on failure.
- **Zoom mode**: Floats a window to fill the workspace rect (not real fullscreen) so swaybar stays visible and Chrome keeps tabs. Tracks a neighbor window for position restoration on unzoom.
