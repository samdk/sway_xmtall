#!/usr/bin/env python3
"""sway-xmonad-tall: an xmonad-like auto-tiler for sway.
Implements a 'tall' layout: primary column on the left,
secondary column on the right."""

import argparse
import logging
import time
import traceback
from typing import Optional

import i3ipc


# -- Per-workspace state ------------------------------------------------------

class WorkspaceState:
  def __init__(self, workspace_id: int, n_lcol: int = 1):
    self.workspace_id = workspace_id
    self.n_lcol = n_lcol
    self.pending_moves = 0
    self.snapshot: Optional[i3ipc.Con] = None
    self.last_rcol_width: Optional[int] = None
    self.zoomed_id: Optional[int] = None
    self.zoom_neighbor_id: Optional[int] = None

  def __repr__(self) -> str:
    return f"WorkspaceState({self.workspace_id}, n_lcol={self.n_lcol})"


WORKSPACES: dict[int, WorkspaceState] = {}


def get_state(i3: i3ipc.Connection, workspace: i3ipc.Con) -> WorkspaceState:
  if workspace.id not in WORKSPACES:
    state = WorkspaceState(workspace.id)
    # Infer n_lcol from existing tree (handles config reload).
    if workspace.nodes and workspace.nodes[0].nodes:
      state.n_lcol = len(workspace.nodes[0].nodes)
    if len(workspace.nodes) >= 2:
      state.last_rcol_width = workspace.nodes[1].rect.width
    WORKSPACES[workspace.id] = state
    logging.debug(f"Created {state} for workspace {workspace.id}.")
  return WORKSPACES[workspace.id]


# -- IPC helpers --------------------------------------------------------------

def get_focused_workspace(i3: i3ipc.Connection) -> Optional[i3ipc.Con]:
  for reply in i3.get_workspaces():
    if reply.focused:
      return i3.get_tree().find_by_id(reply.ipc_data["id"]).workspace()
  return None


def get_focused_window(i3: i3ipc.Connection) -> Optional[i3ipc.Con]:
  ws = get_focused_workspace(i3)
  return ws.find_focused() if ws else None


def get_workspace_of_event(i3: i3ipc.Connection, event: i3ipc.Event) -> Optional[i3ipc.Con]:
  window = i3.get_tree().find_by_id(event.container.id)
  return window.workspace() if window else None


def refetch(i3: i3ipc.Connection, container: i3ipc.Con) -> Optional[i3ipc.Con]:
  return i3.get_tree().find_by_id(container.id)


def is_floating(container: i3ipc.Con) -> bool:
  return container.floating in ['user_on', 'auto_on'] or container.type == "floating_con"


# -- Move helpers -------------------------------------------------------------

MARK = "__sway_xmonad_tall_mark"

def command_move(state: WorkspaceState, node: i3ipc.Con, move_args: str) -> None:
  """Issue a move command on node, suppressing the resulting move event."""
  state.pending_moves += 1
  node.command(f"move {move_args}")

def move_to_target(state: WorkspaceState, node: i3ipc.Con, target: i3ipc.Con) -> None:
  """Move node to be a sibling after target, using a mark."""
  target.command(f"mark --add {MARK}")
  command_move(state, node, f"window to mark {MARK}")
  target.command(f"unmark {MARK}")

def move_before(state: WorkspaceState, node: i3ipc.Con, target: i3ipc.Con) -> None:
  """Move node to just before target in its container."""
  move_to_target(state, node, target)
  command_move(state, node, "up")

def add_to_front(state: WorkspaceState, column: i3ipc.Con, node: i3ipc.Con) -> None:
  """Move node to the front (top) of column."""
  if not column.nodes:
    move_to_target(state, node, column)
    return
  move_before(state, node, column.nodes[0])


# -- Window operations --------------------------------------------------------

def find_offset_window(container: i3ipc.Con, offset: int) -> Optional[i3ipc.Con]:
  """Find the window at `offset` positions from container in workspace leaf order."""
  ws = container.workspace()
  if not ws:
    return None
  leaves = ws.leaves()
  ids = [l.id for l in leaves]
  try:
    idx = ids.index(container.id)
  except ValueError:
    return None  # floating
  return leaves[(idx + offset) % len(leaves)]


def focus_window(i3: i3ipc.Connection, offset: int,
                 window: Optional[i3ipc.Con] = None) -> None:
  focused = window or get_focused_window(i3)
  if not focused:
    return
  target = find_offset_window(focused, offset)
  if target:
    target.command("focus")
    if focused.fullscreen_mode == 1:
      target.command("fullscreen")


def refocus_window(i3: i3ipc.Connection, window: i3ipc.Con) -> None:
  """Re-focus window, warping cursor to its center."""
  # Focusing next then back moves the cursor to the window center,
  # unlike a plain focus which can leave it on the border.
  focus_window(i3, 1, window)
  window.command("focus")
  if window.fullscreen_mode == 1:
    window.command("fullscreen")


def swap_with_offset(i3: i3ipc.Connection, offset: int,
                     window: Optional[i3ipc.Con] = None,
                     focus_after: bool = True) -> None:
  focused = window or get_focused_window(i3)
  if not focused:
    return
  target = find_offset_window(focused, offset)
  if target:
    focused.command(f"swap container with con_id {target.id}")
    if focus_after:
      focused.command("focus")
      if focused.fullscreen_mode == 1:
        target.command("fullscreen")


def promote_window(i3: i3ipc.Connection) -> None:
  ws = get_focused_workspace(i3)
  focused = get_focused_window(i3)
  if not ws or not focused:
    return
  largest = max(ws.leaves(), key=lambda l: l.rect.width * l.rect.height, default=None)
  if not largest:
    return
  focused.command(f"swap container with con_id {largest.id}")
  focused.command("focus")
  if focused.fullscreen_mode == 1:
    focused.command("fullscreen")


# -- Tall layout reflow -------------------------------------------------------

def ensure_two_columns(i3: i3ipc.Connection, state: WorkspaceState,
                       workspace: i3ipc.Con) -> i3ipc.Con:
  """Ensure workspace has exactly 2 top-level splitv containers."""
  # Merge extra columns into column 1.
  while len(workspace.nodes) > 2:
    extra = workspace.nodes[-1]
    target = workspace.nodes[1]
    for node in list(extra.nodes):
      move_to_target(state, node, target)
    workspace = refetch(i3, workspace)

  # Split single column if we have more than n_lcol windows.
  if len(workspace.nodes) == 1:
    col = workspace.nodes[0]
    if len(col.nodes) > state.n_lcol:
      focused = workspace.find_focused()
      command_move(state, col.nodes[-1], "right")
      if focused:
        focused.command("focus")
      workspace = refetch(i3, workspace)

  # Ensure both columns are splitv; restore rcol width if recreating.
  for i, col in enumerate(workspace.nodes):
    if col.layout != "splitv":
      if i == 1 and state.last_rcol_width:
        col.command(f"splitv, resize set width {state.last_rcol_width} px")
      else:
        col.command("splitv")

  return refetch(i3, workspace)


def ensure_single_column(i3: i3ipc.Connection, state: WorkspaceState,
                         workspace: i3ipc.Con) -> i3ipc.Con:
  """Merge all columns into one."""
  while len(workspace.nodes) > 1:
    last = workspace.nodes[-1]
    target = workspace.nodes[0]
    for node in list(last.nodes):
      move_to_target(state, node, target)
    workspace = refetch(i3, workspace)
  return workspace


def reflow(i3: i3ipc.Connection, state: WorkspaceState,
           workspace: i3ipc.Con) -> bool:
  """One pass of structural correction. Returns True if a mutation occurred."""
  leaves = [l for l in workspace.leaves() if not is_floating(l)]

  if len(leaves) <= 1:
    return False

  if state.n_lcol == 0 or len(leaves) <= state.n_lcol:
    if len(workspace.nodes) > 1:
      ensure_single_column(i3, state, workspace)
      return True
    return False

  # Need 2 columns.
  workspace = ensure_two_columns(i3, state, workspace)
  cols = workspace.nodes

  if len(cols) != 2:
    return False

  lcol, rcol = cols[0], cols[1]

  # Save rcol width.
  state.last_rcol_width = rcol.rect.width

  # Balance: move windows between columns.
  if len(lcol.nodes) < state.n_lcol and rcol.nodes:
    move_to_target(state, rcol.nodes[0], lcol)
    return True

  if len(lcol.nodes) > state.n_lcol and len(lcol.nodes) > 1:
    add_to_front(state, rcol, lcol.nodes[-1])
    return True

  return False


def do_reflow(i3: i3ipc.Connection, state: WorkspaceState) -> None:
  """Run reflow until the layout is correct."""
  for _ in range(20):  # safety bound
    workspace = i3.get_tree().find_by_id(state.workspace_id)
    if not workspace:
      return
    if not reflow(i3, state, workspace):
      break


# -- Event handlers -----------------------------------------------------------

def on_window_new(i3: i3ipc.Connection, event: i3ipc.Event) -> None:
  workspace = get_workspace_of_event(i3, event) or get_focused_workspace(i3)
  if not workspace:
    return
  state = get_state(i3, workspace)

  try:
    i3.enable_command_buffering()

    # Re-fetch to handle the dialog/floating race: sway creates dialogs as
    # tiling then immediately floats them.
    workspace = refetch(i3, workspace)
    old_leaf_ids = {l.id for l in state.snapshot.leaves()} if state.snapshot else set()
    leaf_ids = {l.id for l in workspace.leaves()}

    should_reflow = False
    post_hooks = []

    if old_leaf_ids != leaf_ids:
      # Swap new window with prev so it takes the "current" position.
      new_window = workspace.find_by_id(event.container.id)
      if new_window:
        swap_with_offset(i3, -1, window=new_window)
      should_reflow = True

    # Handle fullscreen new windows.
    con = workspace.find_by_id(event.container.id)
    if con and con.fullscreen_mode == 1:
      post_hooks.append(lambda: con.command("focus"))
      post_hooks.append(lambda: con.command("fullscreen"))

    if should_reflow:
      do_reflow(i3, state)

      workspace = refetch(i3, workspace)
      if (workspace and
          workspace.id == get_focused_workspace(i3).id and
          (focused := workspace.find_focused())):
        refocus_window(i3, focused)

    for hook in post_hooks:
      hook()

    state.snapshot = refetch(i3, workspace) if workspace else None
    i3.disable_command_buffering()
  except Exception:
    traceback.print_exc()


def on_window_close(i3: i3ipc.Connection, event: i3ipc.Event) -> None:
  # The closed window's workspace comes from the snapshot, since the window
  # is already gone from the live tree.
  workspace = get_focused_workspace(i3)
  if not workspace:
    return
  state = get_state(i3, workspace)

  try:
    i3.enable_command_buffering()

    # Clear zoom state if the zoomed window or its neighbor was closed
    if state.zoomed_id is not None and event.container.id == state.zoomed_id:
      state.zoomed_id = None
      state.zoom_neighbor_id = None
    if state.zoom_neighbor_id is not None and event.container.id == state.zoom_neighbor_id:
      state.zoom_neighbor_id = None

    should_reflow = False
    post_hooks = []

    old_leaf_ids = {l.id for l in state.snapshot.leaves()} if state.snapshot else set()
    leaf_ids = {l.id for l in workspace.leaves()}

    closed = state.snapshot.find_by_id(event.container.id) if state.snapshot else None

    if (old_leaf_ids != leaf_ids and
        workspace.id == get_focused_workspace(i3).id and
        closed and not is_floating(closed)):
      should_reflow = True

      # Focus the "next" window instead of sway's default.
      was_fullscreen = closed.fullscreen_mode == 1
      old_leaves = state.snapshot.leaves()
      old_ids = [l.id for l in old_leaves]
      if closed.id in old_ids:
        idx = old_ids.index(closed.id)
        for offset in range(1, len(old_ids) + 1):
          candidate = old_leaves[(idx + offset) % len(old_ids)]
          if candidate.id in leaf_ids:
            candidate.command("focus")
            if was_fullscreen:
              post_hooks.append(lambda: candidate.command("fullscreen"))
            break

    if should_reflow:
      do_reflow(i3, state)

      workspace = refetch(i3, workspace)
      if (workspace and
          workspace.id == get_focused_workspace(i3).id and
          (focused := workspace.find_focused())):
        refocus_window(i3, focused)

    for hook in post_hooks:
      hook()

    # Clean up state for empty workspaces.
    workspace = refetch(i3, workspace)
    if workspace and not workspace.leaves():
      WORKSPACES.pop(workspace.id, None)
    else:
      state.snapshot = workspace
    i3.disable_command_buffering()
  except Exception:
    traceback.print_exc()


def on_window_move(i3: i3ipc.Connection, event: i3ipc.Event) -> None:
  workspace = get_workspace_of_event(i3, event) or get_focused_workspace(i3)
  if not workspace:
    return
  state = get_state(i3, workspace)

  if state.pending_moves > 0:
    logging.debug(f"Suppressing self-triggered move ({state.pending_moves} pending).")
    state.pending_moves -= 1
    return

  try:
    i3.enable_command_buffering()

    # Swap moved window with prev to take its new position.
    window = workspace.find_by_id(event.container.id)
    if window:
      swap_with_offset(i3, -1, window=window, focus_after=False)

    # Reflow the old workspace (the one the window came from).
    reflow_old_workspace(i3, workspace)

    do_reflow(i3, state)

    # Refocus the current workspace (split commands can steal focus).
    focused_workspace = get_focused_workspace(i3)
    if focused_workspace:
      i3.command(f"workspace {focused_workspace.name}")

    state.snapshot = refetch(i3, workspace)
    i3.disable_command_buffering()
  except Exception:
    traceback.print_exc()


def reflow_old_workspace(i3: i3ipc.Connection, new_workspace: i3ipc.Con) -> None:
  """Reflow the workspace a window was moved from."""
  old_workspace = get_focused_workspace(i3)
  if not old_workspace:
    return

  # For cross-output moves, focused workspace may be the same as new.
  if old_workspace.id == new_workspace.id:
    i3.command("workspace back_and_forth")
    old_workspace = get_focused_workspace(i3)
    i3.command("workspace back_and_forth")

  if old_workspace:
    old_state = get_state(i3, old_workspace)
    do_reflow(i3, old_state)
    old_state.snapshot = refetch(i3, old_workspace)


# -- Command handlers ---------------------------------------------------------

def cmd_promote(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  promote_window(i3)

def cmd_focus_next(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  focus_window(i3, 1)

def cmd_focus_prev(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  focus_window(i3, -1)

def cmd_swap_next(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  swap_with_offset(i3, 1)

def cmd_swap_prev(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  swap_with_offset(i3, -1)

def adjust_n_lcol(i3: i3ipc.Connection, delta: int) -> None:
  ws = get_focused_workspace(i3)
  if not ws:
    return
  state = get_state(i3, ws)
  focused = get_focused_window(i3)
  n_leaves = len([l for l in ws.leaves() if not is_floating(l)])
  effective = max(0, min(state.n_lcol, n_leaves))
  state.n_lcol = max(0, min(effective + delta, n_leaves))
  logging.debug(f"adjust_n_lcol: n_lcol={state.n_lcol}")
  do_reflow(i3, state)
  if focused:
    focused.command("focus")
  state.snapshot = refetch(i3, ws)

def cmd_flow_left(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  adjust_n_lcol(i3, 1)

def cmd_flow_right(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  adjust_n_lcol(i3, -1)

def cmd_move_divider(i3: i3ipc.Connection, event: i3ipc.Event, direction: str, amount: str = "50px", *args) -> None:
  ws = get_focused_workspace(i3)
  if not ws or len(ws.nodes) < 2:
    return
  state = get_state(i3, ws)
  lcol = ws.nodes[0]
  if direction == "right":
    lcol.command(f"resize grow width {amount}")
  elif direction == "left":
    lcol.command(f"resize shrink width {amount}")
  ws = refetch(i3, ws)
  if ws and len(ws.nodes) >= 2:
    state.last_rcol_width = ws.nodes[1].rect.width

def cmd_zoom(i3: i3ipc.Connection, event: i3ipc.Event, *args) -> None:
  ws = get_focused_workspace(i3)
  if not ws:
    return
  state = get_state(i3, ws)
  focused = get_focused_window(i3)
  if not focused:
    return

  # Toggle off: unzoom
  if state.zoomed_id is not None:
    zoomed = i3.get_tree().find_by_id(state.zoomed_id)
    if zoomed:
      zoomed.command("floating disable, border pixel 2")
      do_reflow(i3, state)
      # Restore position next to saved neighbor
      if state.zoom_neighbor_id is not None:
        ws = refetch(i3, ws)
        if ws:
          leaf_ids = [l.id for l in ws.leaves() if not is_floating(l)]
          zoomed = refetch(i3, zoomed)
          if zoomed and state.zoom_neighbor_id in leaf_ids:
            neighbor = i3.get_tree().find_by_id(state.zoom_neighbor_id)
            if neighbor:
              move_before(state, zoomed, neighbor)
              do_reflow(i3, state)
      zoomed = refetch(i3, zoomed)
      if zoomed:
        zoomed.command("focus")
    state.zoomed_id = None
    state.zoom_neighbor_id = None
    state.snapshot = refetch(i3, ws)
    return

  # Toggle on: zoom
  state.zoomed_id = focused.id
  # Find next window in tiling order for position restore (without wrapping)
  leaves = ws.leaves()
  leaf_ids = [l.id for l in leaves]
  try:
    idx = leaf_ids.index(focused.id)
    state.zoom_neighbor_id = leaf_ids[idx + 1] if idx + 1 < len(leaf_ids) else None
  except ValueError:
    state.zoom_neighbor_id = None
  # Float and fill workspace rect
  r = ws.rect
  focused.command(
    f"floating enable, border none, "
    f"resize set {r.width} px {r.height} px, "
    f"move absolute position {r.x} px {r.y} px"
  )
  do_reflow(i3, state)
  state.snapshot = refetch(i3, ws)


COMMANDS = {
  "promote_window": cmd_promote,
  "focus_next_window": cmd_focus_next,
  "focus_prev_window": cmd_focus_prev,
  "swap_with_next_window": cmd_swap_next,
  "swap_with_prev_window": cmd_swap_prev,
  "flow_left": cmd_flow_left,
  "flow_right": cmd_flow_right,
  "fullscreen": cmd_zoom,
  "move_divider": cmd_move_divider,
}


def on_binding(i3: i3ipc.Connection, event: i3ipc.Event) -> None:
  parts = event.binding.command.split()
  if not parts or parts[0] != "nop":
    return
  cmd_name = parts[1] if len(parts) > 1 else None
  if not cmd_name:
    return

  handler = COMMANDS.get(cmd_name)
  if not handler:
    return

  try:
    i3.enable_command_buffering()
    handler(i3, event, *parts[2:])
    i3.disable_command_buffering()
  except Exception:
    traceback.print_exc()


# -- Connection with command buffering ----------------------------------------

class Connection(i3ipc.Connection):

  def __init__(self, *args, **kwargs) -> None:
    super().__init__(*args, **kwargs)
    self.buffering_commands = False
    self.command_buffer: list[str] = []

  def command(self, payload: str) -> list[i3ipc.CommandReply]:
    if self.buffering_commands:
      logging.debug(f"Buffering: {payload}")
      self.command_buffer.append(payload)
      return []
    logging.debug(f"Executing: {payload}")
    if cmd_args.delay:
      time.sleep(cmd_args.delay)
    return super().command(payload)

  def enable_command_buffering(self) -> None:
    self.buffering_commands = True

  def disable_command_buffering(self) -> list[i3ipc.CommandReply]:
    self.buffering_commands = False
    if not self.command_buffer:
      return []
    payload = ";".join(self.command_buffer)
    self.command_buffer = []
    return self.command(payload)

  def get_tree(self) -> i3ipc.Con:
    self.disable_command_buffering()
    tree = super().get_tree()
    self.enable_command_buffering()
    return tree

  def get_workspaces(self) -> list[i3ipc.replies.WorkspaceReply]:
    self.disable_command_buffering()
    workspaces = super().get_workspaces()
    self.enable_command_buffering()
    return workspaces


# -- Main ---------------------------------------------------------------------

argparser = argparse.ArgumentParser(description='sway-xmonad-tall: an xmonad-like auto-tiler for sway.')
argparser.add_argument('--verbose', '-v', action='count', help="Enable debug logging.")
argparser.add_argument('--log-file', help="Log file path (default: stderr).")
argparser.add_argument('--delay', default=0.0, type=float,
                       help="Sleep between commands (debug).")
cmd_args = argparser.parse_args()

if __name__ == "__main__":
  logging.basicConfig(
    level=logging.DEBUG if cmd_args.verbose else logging.WARNING,
    filename=cmd_args.log_file,
    format='%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s')

  i3 = Connection()

  i3.on(i3ipc.Event.BINDING, on_binding)
  i3.on(i3ipc.Event.WINDOW_NEW, on_window_new)
  i3.on(i3ipc.Event.WINDOW_CLOSE, on_window_close)
  i3.on(i3ipc.Event.WINDOW_MOVE, on_window_move)

  i3.main()
