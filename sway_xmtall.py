#!/usr/bin/env python3
"""sway-xmtall: an xmonad-like auto-tiler for sway.
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

MARK = "__sway_xmtall_mark"

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

def reflow_workspace(i3: i3ipc.Connection, state: WorkspaceState,
                     workspace: i3ipc.Con) -> None:
  """Full reflow of workspace layout with minimal IPC round-trips.
  Computes all needed moves from the current tree snapshot and batches them,
  avoiding the per-move refetch cycle."""
  leaves = [l for l in workspace.leaves() if not is_floating(l)]

  if len(leaves) <= 1:
    return

  # All windows fit in one column — merge everything (batched).
  if state.n_lcol == 0 or len(leaves) <= state.n_lcol:
    if len(workspace.nodes) > 1:
      first_col = workspace.nodes[0]
      prev_target = first_col.nodes[-1] if first_col.nodes else first_col
      for col in workspace.nodes[1:]:
        for node in list(col.nodes):
          move_to_target(state, node, prev_target)
          prev_target = node
    return

  # Need two columns.
  cols = workspace.nodes

  # Create second column from single column (requires refetch for new IDs).
  if len(cols) == 1:
    col = cols[0]
    if len(col.nodes) > state.n_lcol:
      focused = workspace.find_focused()
      command_move(state, col.nodes[-1], "right")
      if focused:
        focused.command("focus")
      workspace = refetch(i3, workspace)
      if not workspace:
        return
      cols = workspace.nodes

  # Merge extra columns (>2) into column 1 (batched, one refetch after).
  if len(cols) > 2:
    target_col = cols[1]
    prev_target = target_col.nodes[-1] if target_col.nodes else target_col
    for col in list(cols[2:]):
      for node in list(col.nodes):
        move_to_target(state, node, prev_target)
        prev_target = node
    workspace = refetch(i3, workspace)
    if not workspace:
      return
    cols = workspace.nodes

  if len(cols) != 2:
    return

  # Ensure both columns are splitv; restore rcol width if recreating.
  for i, col in enumerate(cols):
    if col.layout != "splitv":
      if i == 1 and state.last_rcol_width:
        col.command(f"splitv, resize set width {state.last_rcol_width} px")
      else:
        col.command("splitv")

  lcol, rcol = cols[0], cols[1]
  state.last_rcol_width = rcol.rect.width

  # Balance columns — batch all moves without intermediate refetches.
  if len(lcol.nodes) < state.n_lcol and rcol.nodes:
    # Pull windows from rcol to lcol.
    needed = min(state.n_lcol - len(lcol.nodes), len(rcol.nodes))
    to_move = list(rcol.nodes[:needed])
    prev_target = lcol.nodes[-1] if lcol.nodes else lcol
    for node in to_move:
      move_to_target(state, node, prev_target)
      prev_target = node

  elif len(lcol.nodes) > state.n_lcol and len(lcol.nodes) > 1:
    # Push excess windows from lcol to rcol front.
    excess = list(lcol.nodes[state.n_lcol:])
    if rcol.nodes:
      # Insert before first rcol node, in reverse order to preserve ordering.
      move_before(state, excess[-1], rcol.nodes[0])
      for i in range(len(excess) - 2, -1, -1):
        move_before(state, excess[i], excess[i + 1])
    else:
      # rcol is empty — move first excess in, rest after it.
      move_to_target(state, excess[0], rcol)
      for i in range(1, len(excess)):
        move_to_target(state, excess[i], excess[i - 1])


def check_reflow(state: WorkspaceState, workspace: i3ipc.Con) -> bool:
  """Check whether the workspace layout matches the intended structure.
  Returns True if the layout is correct."""
  leaves = [l for l in workspace.leaves() if not is_floating(l)]
  n = len(leaves)
  cols = workspace.nodes

  if n <= 1:
    return True

  # Should be single column.
  if state.n_lcol == 0 or n <= state.n_lcol:
    return len(cols) == 1

  # Should be two columns with n_lcol in the left.
  if len(cols) != 2:
    return False
  lcol_leaves = [l for l in cols[0].leaves() if not is_floating(l)]
  return len(lcol_leaves) == state.n_lcol


def correct_reflow(i3: i3ipc.Connection, state: WorkspaceState,
                   workspace: i3ipc.Con) -> None:
  """Single corrective move. Handles one structural issue per call."""
  leaves = [l for l in workspace.leaves() if not is_floating(l)]
  if len(leaves) <= 1:
    return
  cols = workspace.nodes

  if state.n_lcol == 0 or len(leaves) <= state.n_lcol:
    if len(cols) > 1:
      for node in list(cols[-1].nodes):
        move_to_target(state, node, cols[0])
    return

  if len(cols) > 2:
    for node in list(cols[-1].nodes):
      move_to_target(state, node, cols[1])
    return

  if len(cols) == 1:
    col = cols[0]
    if len(col.nodes) > state.n_lcol:
      command_move(state, col.nodes[-1], "right")
    return

  if len(cols) == 2:
    lcol, rcol = cols[0], cols[1]
    lcol_leaves = [l for l in lcol.leaves() if not is_floating(l)]
    if len(lcol_leaves) < state.n_lcol and rcol.nodes:
      move_to_target(state, rcol.nodes[0], lcol)
    elif len(lcol_leaves) > state.n_lcol and len(lcol.nodes) > 1:
      add_to_front(state, rcol, lcol.nodes[-1])


def do_reflow(i3: i3ipc.Connection, state: WorkspaceState,
              workspace: Optional[i3ipc.Con] = None) -> None:
  """Reflow workspace layout. Callers should verify with check_reflow after
  flushing commands (e.g. via refetch) if they need to confirm the result."""
  if workspace is None:
    workspace = i3.get_tree().find_by_id(state.workspace_id)
  if not workspace:
    return
  reflow_workspace(i3, state, workspace)


# -- Event handlers -----------------------------------------------------------

def speculative_swap_and_reflow(state: WorkspaceState,
                                workspace: i3ipc.Con, new_id: int) -> bool:
  """Compute swap+reflow from workspace snapshot without extra IPC.
  Predicts post-swap column order to batch swap+reflow together.
  Returns True if speculative commands were issued, False to fall back."""
  new_window = workspace.find_by_id(new_id)
  if not new_window or is_floating(new_window):
    return False

  target = find_offset_window(new_window, -1)
  if not target or target.id == new_window.id:
    return False

  leaves = [l for l in workspace.leaves() if not is_floating(l)]

  # Only speculate for the stable 2-column case.
  if state.n_lcol == 0 or len(leaves) <= state.n_lcol:
    return False
  cols = workspace.nodes
  if len(cols) != 2:
    return False

  lcol, rcol = cols[0], cols[1]

  # Issue the swap.
  new_window.command(f"swap container with con_id {target.id}")
  new_window.command("focus")

  state.last_rcol_width = rcol.rect.width

  # Only speculate when both nodes are direct children of the same column.
  lcol_node_ids = {n.id for n in lcol.nodes}
  rcol_node_ids = {n.id for n in rcol.nodes}

  if new_window.id in lcol_node_ids and target.id in lcol_node_ids:
    # Same-column swap in lcol. Simulate post-swap node order.
    nodes = list(lcol.nodes)
    idx_new = next(k for k, n in enumerate(nodes) if n.id == new_window.id)
    idx_target = next(k for k, n in enumerate(nodes) if n.id == target.id)
    nodes[idx_new], nodes[idx_target] = nodes[idx_target], nodes[idx_new]

    if len(nodes) > state.n_lcol:
      excess = nodes[state.n_lcol:]
      if rcol.nodes:
        move_before(state, excess[-1], rcol.nodes[0])
        for i in range(len(excess) - 2, -1, -1):
          move_before(state, excess[i], excess[i + 1])
      else:
        move_to_target(state, excess[0], rcol)
        for i in range(1, len(excess)):
          move_to_target(state, excess[i], excess[i - 1])
    return True

  if new_window.id in rcol_node_ids and target.id in rcol_node_ids:
    # Same-column swap in rcol. Lcol count unchanged, no reflow needed.
    return True

  # Cross-column swap or unexpected structure: fall back.
  return False


def on_window_new(i3: i3ipc.Connection, event: i3ipc.Event) -> None:
  workspace = get_workspace_of_event(i3, event) or get_focused_workspace(i3)
  if not workspace:
    return
  state = get_state(i3, workspace)

  try:
    i3.enable_command_buffering()

    old_leaf_ids = {l.id for l in state.snapshot.leaves()} if state.snapshot else set()
    leaf_ids = {l.id for l in workspace.leaves()}

    should_reflow = False
    post_hooks = []

    if old_leaf_ids != leaf_ids:
      # Try speculative path: compute swap+reflow from snapshot, batch together.
      if not speculative_swap_and_reflow(state, workspace, event.container.id):
        # Fallback: swap, flush to see post-swap state, then plan reflow.
        new_window = workspace.find_by_id(event.container.id)
        if new_window:
          swap_with_offset(i3, -1, window=new_window)
        workspace = refetch(i3, workspace)
        do_reflow(i3, state, workspace)
      should_reflow = True

    # Handle fullscreen new windows.
    con = workspace.find_by_id(event.container.id)
    if con and con.fullscreen_mode == 1:
      post_hooks.append(lambda: con.command("focus"))
      post_hooks.append(lambda: con.command("fullscreen"))

    if should_reflow:
      # Flush all commands, verify, refocus.
      workspace = refetch(i3, workspace)
      if workspace and not check_reflow(state, workspace):
        logging.debug("Post-reflow check failed, correcting.")
        correct_reflow(i3, state, workspace)
        workspace = refetch(i3, workspace)

      if workspace and (focused := workspace.find_focused()):
        refocus_window(i3, focused)

    for hook in post_hooks:
      hook()

    state.snapshot = workspace
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

    leaf_ids = {l.id for l in workspace.leaves()}
    closed = state.snapshot.find_by_id(event.container.id) if state.snapshot else None

    if state.snapshot is None:
      # No snapshot (e.g., after script restart). Reflow to ensure correct structure.
      should_reflow = True
    elif closed and not is_floating(closed):
      old_leaf_ids = {l.id for l in state.snapshot.leaves()}
      if old_leaf_ids != leaf_ids:
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
      do_reflow(i3, state, workspace)

      workspace = refetch(i3, workspace)
      if workspace and not check_reflow(state, workspace):
        logging.debug("Post-reflow check failed, correcting.")
        correct_reflow(i3, state, workspace)
        workspace = refetch(i3, workspace)

      if workspace and (focused := workspace.find_focused()):
        refocus_window(i3, focused)

    for hook in post_hooks:
      hook()

    # Clean up state for empty workspaces.
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
  focused = ws.find_focused()
  n_leaves = len([l for l in ws.leaves() if not is_floating(l)])
  effective = max(0, min(state.n_lcol, n_leaves))
  state.n_lcol = max(0, min(effective + delta, n_leaves))
  logging.debug(f"adjust_n_lcol: n_lcol={state.n_lcol}")
  do_reflow(i3, state, ws)
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

argparser = argparse.ArgumentParser(description='sway-xmtall: an xmonad-like auto-tiler for sway.')
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

  # Initialize state for existing workspaces so snapshots are valid
  # from the first event (handles script restart / exec_always).
  tree = i3.get_tree()
  for reply in i3.get_workspaces():
    node = tree.find_by_id(reply.ipc_data["id"])
    if node:
      ws = node.workspace()
      if ws and ws.leaves():
        get_state(i3, ws).snapshot = ws

  i3.on(i3ipc.Event.BINDING, on_binding)
  i3.on(i3ipc.Event.WINDOW_NEW, on_window_new)
  i3.on(i3ipc.Event.WINDOW_CLOSE, on_window_close)
  i3.on(i3ipc.Event.WINDOW_MOVE, on_window_move)

  i3.main()
