//! sway-xmtall: an xmonad-like auto-tiler for sway.
//! Implements a "tall" layout: primary column on the left,
//! secondary column on the right.
//!
//! This is a faithful port of the Python sway_xmtall.py.

use clap::Parser;
use log::{debug, warn};
use std::collections::HashMap;
use std::thread;
use std::time::Duration;
use swayipc::{Connection, Event, EventType, Node, NodeLayout, NodeType};

// ---------------------------------------------------------------------------
// CLI arguments
// ---------------------------------------------------------------------------

#[derive(Parser, Debug)]
#[command(about = "sway-xmtall: an xmonad-like auto-tiler for sway.")]
struct Args {
    /// Enable debug logging (repeat for more verbosity).
    #[arg(short, long, action = clap::ArgAction::Count)]
    verbose: u8,

    /// Log file path (default: stderr).
    #[arg(long)]
    log_file: Option<String>,

    /// Sleep between commands (seconds, debug).
    #[arg(long, default_value_t = 0.0)]
    delay: f64,
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MARK: &str = "__sway_xmtall_mark";

// ---------------------------------------------------------------------------
// Per-workspace state
// ---------------------------------------------------------------------------

struct WorkspaceState {
    workspace_id: i64,
    n_lcol: usize,
    pending_moves: usize,
    snapshot: Option<Node>,
    last_rcol_width: Option<i32>,
    zoomed_id: Option<i64>,
    zoom_neighbor_id: Option<i64>,
}

impl WorkspaceState {
    fn new(workspace_id: i64) -> Self {
        Self {
            workspace_id,
            n_lcol: 1,
            pending_moves: 0,
            snapshot: None,
            last_rcol_width: None,
            zoomed_id: None,
            zoom_neighbor_id: None,
        }
    }
}

impl std::fmt::Debug for WorkspaceState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "WorkspaceState({}, n_lcol={})",
            self.workspace_id, self.n_lcol
        )
    }
}

// ---------------------------------------------------------------------------
// Connection wrapper with command buffering
// ---------------------------------------------------------------------------

struct Conn {
    inner: Connection,
    buffering: bool,
    buffer: Vec<String>,
    delay: f64,
}

impl Conn {
    fn new(delay: f64) -> Self {
        Self {
            inner: Connection::new().expect("Failed to connect to sway IPC"),
            buffering: false,
            buffer: Vec::new(),
            delay,
        }
    }

    /// Send a command (or buffer it).
    fn command(&mut self, payload: &str) {
        if self.buffering {
            debug!("Buffering: {}", payload);
            self.buffer.push(payload.to_string());
        } else {
            debug!("Executing: {}", payload);
            if self.delay > 0.0 {
                thread::sleep(Duration::from_secs_f64(self.delay));
            }
            if let Err(e) = self.inner.run_command(payload) {
                warn!("run_command error: {}", e);
            }
        }
    }

    /// Send a command targeted at a specific container by con_id.
    fn command_on(&mut self, con_id: i64, cmd: &str) {
        self.command(&format!("[con_id={}] {}", con_id, cmd));
    }

    fn enable_buffering(&mut self) {
        self.buffering = true;
    }

    fn disable_buffering(&mut self) {
        self.buffering = false;
        if self.buffer.is_empty() {
            return;
        }
        let payload = self.buffer.join(";");
        self.buffer.clear();
        self.command(&payload);
    }

    /// Flush buffered commands, then get_tree, then re-enable buffering.
    fn get_tree(&mut self) -> Node {
        self.disable_buffering();
        let tree = self.inner.get_tree().expect("get_tree failed");
        self.enable_buffering();
        tree
    }

    /// Flush buffered commands, then get_workspaces, then re-enable buffering.
    fn get_workspaces(&mut self) -> Vec<swayipc::Workspace> {
        self.disable_buffering();
        let ws = self.inner.get_workspaces().expect("get_workspaces failed");
        self.enable_buffering();
        ws
    }
}

// ---------------------------------------------------------------------------
// Node helpers (equivalent to i3ipc.Con methods)
// ---------------------------------------------------------------------------

/// Recursively collect all leaf (window) nodes.
fn leaves(node: &Node) -> Vec<&Node> {
    let mut result = Vec::new();
    leaves_inner(node, &mut result);
    result
}

fn leaves_inner<'a>(node: &'a Node, out: &mut Vec<&'a Node>) {
    if node.nodes.is_empty() && node.floating_nodes.is_empty() {
        // Leaf node.
        out.push(node);
        return;
    }
    for child in &node.nodes {
        leaves_inner(child, out);
    }
    for child in &node.floating_nodes {
        leaves_inner(child, out);
    }
}

/// Find a node by id in a subtree.
fn find_by_id(root: &Node, id: i64) -> Option<&Node> {
    if root.id == id {
        return Some(root);
    }
    for child in &root.nodes {
        if let Some(found) = find_by_id(child, id) {
            return Some(found);
        }
    }
    for child in &root.floating_nodes {
        if let Some(found) = find_by_id(child, id) {
            return Some(found);
        }
    }
    None
}

/// Find the focused node within a subtree.
fn find_focused(root: &Node) -> Option<&Node> {
    if root.focused {
        return Some(root);
    }
    for child in &root.nodes {
        if let Some(found) = find_focused(child) {
            return Some(found);
        }
    }
    for child in &root.floating_nodes {
        if let Some(found) = find_focused(child) {
            return Some(found);
        }
    }
    None
}

/// Walk up to find the workspace of a node (from a full tree).
/// Since we don't have parent pointers, we search the tree for a workspace
/// containing the given id.
fn find_workspace_of(root: &Node, target_id: i64) -> Option<&Node> {
    // root -> outputs -> workspaces -> ...
    for output in &root.nodes {
        for ws_or_content in &output.nodes {
            if ws_or_content.node_type == NodeType::Workspace {
                if find_by_id(ws_or_content, target_id).is_some() {
                    return Some(ws_or_content);
                }
            }
            // Some trees have a "content" container between output and workspace.
            for ws in &ws_or_content.nodes {
                if ws.node_type == NodeType::Workspace {
                    if find_by_id(ws, target_id).is_some() {
                        return Some(ws);
                    }
                }
            }
        }
    }
    None
}

fn is_floating(node: &Node) -> bool {
    node.node_type == NodeType::FloatingCon
        || matches!(
            node.floating,
            Some(swayipc::Floating::UserOn) | Some(swayipc::Floating::AutoOn)
        )
}

/// Get the focused workspace from connection.
fn get_focused_workspace_node(conn: &mut Conn) -> Option<i64> {
    let workspaces = conn.get_workspaces();
    for ws in &workspaces {
        if ws.focused {
            return Some(ws.id);
        }
    }
    None
}

/// Get the focused workspace Node from the tree.
fn get_focused_workspace(conn: &mut Conn) -> Option<Node> {
    let ws_id = get_focused_workspace_node(conn)?;
    let tree = conn.get_tree();
    find_by_id(&tree, ws_id).cloned()
}

/// Get the focused window.
fn get_focused_window(conn: &mut Conn) -> Option<Node> {
    let ws = get_focused_workspace(conn)?;
    find_focused(&ws).cloned()
}

/// Re-fetch a node from a fresh tree by id.
fn refetch(conn: &mut Conn, id: i64) -> Option<Node> {
    let tree = conn.get_tree();
    find_by_id(&tree, id).cloned()
}

/// Get workspace for an event container.
fn get_workspace_of_event(conn: &mut Conn, container_id: i64) -> Option<Node> {
    let tree = conn.get_tree();
    let ws = find_workspace_of(&tree, container_id)?;
    Some(ws.clone())
}

// ---------------------------------------------------------------------------
// State management
// ---------------------------------------------------------------------------

fn get_state(workspaces: &mut HashMap<i64, WorkspaceState>, ws: &Node) -> &mut WorkspaceState {
    let ws_id = ws.id;
    workspaces.entry(ws_id).or_insert_with(|| {
        let mut state = WorkspaceState::new(ws_id);
        // Infer n_lcol from existing tree (handles config reload).
        if !ws.nodes.is_empty() && !ws.nodes[0].nodes.is_empty() {
            state.n_lcol = ws.nodes[0].nodes.len();
        }
        if ws.nodes.len() >= 2 {
            state.last_rcol_width = Some(ws.nodes[1].rect.width);
        }
        debug!("Created {:?} for workspace {}.", state, ws_id);
        state
    })
}

// ---------------------------------------------------------------------------
// Move helpers
// ---------------------------------------------------------------------------

fn command_move(conn: &mut Conn, state: &mut WorkspaceState, node_id: i64, move_args: &str) {
    state.pending_moves += 1;
    conn.command_on(node_id, &format!("move {}", move_args));
}

fn move_to_target(conn: &mut Conn, state: &mut WorkspaceState, node_id: i64, target_id: i64) {
    conn.command_on(target_id, &format!("mark --add {}", MARK));
    command_move(conn, state, node_id, &format!("window to mark {}", MARK));
    conn.command_on(target_id, &format!("unmark {}", MARK));
}

fn move_before(conn: &mut Conn, state: &mut WorkspaceState, node_id: i64, target_id: i64) {
    move_to_target(conn, state, node_id, target_id);
    command_move(conn, state, node_id, "up");
}

fn add_to_front(conn: &mut Conn, state: &mut WorkspaceState, column: &Node, node_id: i64) {
    if column.nodes.is_empty() {
        move_to_target(conn, state, node_id, column.id);
        return;
    }
    move_before(conn, state, node_id, column.nodes[0].id);
}

// ---------------------------------------------------------------------------
// Window operations
// ---------------------------------------------------------------------------

fn find_offset_window(ws: &Node, container_id: i64, offset: i32) -> Option<i64> {
    let lvs = leaves(ws);
    let ids: Vec<i64> = lvs.iter().map(|l| l.id).collect();
    let idx = ids.iter().position(|&id| id == container_id)?;
    let len = ids.len() as i32;
    let new_idx = ((idx as i32 + offset) % len + len) % len;
    Some(ids[new_idx as usize])
}

fn focus_window(conn: &mut Conn, offset: i32, window: Option<&Node>) {
    let focused = match window {
        Some(w) => w.clone(),
        None => match get_focused_window(conn) {
            Some(w) => w,
            None => return,
        },
    };

    let ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };

    let target_id = match find_offset_window(&ws, focused.id, offset) {
        Some(id) => id,
        None => return,
    };

    conn.command_on(target_id, "focus");
    if focused.fullscreen_mode == Some(1) {
        conn.command_on(target_id, "fullscreen");
    }
}

fn refocus_window(conn: &mut Conn, window_id: i64, fullscreen: bool) {
    // Focusing next then back moves the cursor to the window center.
    let ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };
    if let Some(next_id) = find_offset_window(&ws, window_id, 1) {
        conn.command_on(next_id, "focus");
    }
    conn.command_on(window_id, "focus");
    if fullscreen {
        conn.command_on(window_id, "fullscreen");
    }
}

fn swap_with_offset(conn: &mut Conn, offset: i32, window: Option<&Node>, focus_after: bool) {
    let focused = match window {
        Some(w) => w.clone(),
        None => match get_focused_window(conn) {
            Some(w) => w,
            None => return,
        },
    };

    let ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };

    let target_id = match find_offset_window(&ws, focused.id, offset) {
        Some(id) => id,
        None => return,
    };

    conn.command_on(
        focused.id,
        &format!("swap container with con_id {}", target_id),
    );
    if focus_after {
        conn.command_on(focused.id, "focus");
        if focused.fullscreen_mode == Some(1) {
            conn.command_on(target_id, "fullscreen");
        }
    }
}

fn promote_window(conn: &mut Conn) {
    let ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };
    let focused = match find_focused(&ws) {
        Some(f) => f.clone(),
        None => return,
    };
    let lvs = leaves(&ws);
    let largest = match lvs
        .iter()
        .max_by_key(|l| (l.rect.width as i64) * (l.rect.height as i64))
    {
        Some(l) => *l,
        None => return,
    };
    conn.command_on(
        focused.id,
        &format!("swap container with con_id {}", largest.id),
    );
    conn.command_on(focused.id, "focus");
    if focused.fullscreen_mode == Some(1) {
        conn.command_on(focused.id, "fullscreen");
    }
}

// ---------------------------------------------------------------------------
// Tall layout reflow
// ---------------------------------------------------------------------------

fn ensure_two_columns(conn: &mut Conn, state: &mut WorkspaceState, ws_id: i64) -> Option<Node> {
    let mut workspace = refetch(conn, ws_id)?;

    // Merge extra columns into column 1.
    while workspace.nodes.len() > 2 {
        let extra_id = workspace.nodes.last().unwrap().id;
        let target_id = workspace.nodes[1].id;
        let extra_child_ids: Vec<i64> = {
            let extra = find_by_id(&workspace, extra_id)?;
            extra.nodes.iter().map(|n| n.id).collect()
        };
        for child_id in extra_child_ids {
            move_to_target(conn, state, child_id, target_id);
        }
        workspace = refetch(conn, ws_id)?;
    }

    // Split single column if we have more than n_lcol windows.
    if workspace.nodes.len() == 1 {
        let col = &workspace.nodes[0];
        if col.nodes.len() > state.n_lcol {
            let focused_id = find_focused(&workspace).map(|f| f.id);
            let last_id = col.nodes.last().unwrap().id;
            command_move(conn, state, last_id, "right");
            if let Some(fid) = focused_id {
                conn.command_on(fid, "focus");
            }
            workspace = refetch(conn, ws_id)?;
        }
    }

    // Ensure both columns are splitv; restore rcol width if recreating.
    for (i, col) in workspace.nodes.iter().enumerate() {
        if col.layout != NodeLayout::SplitV {
            if i == 1 {
                if let Some(w) = state.last_rcol_width {
                    conn.command_on(col.id, &format!("splitv, resize set width {} px", w));
                } else {
                    conn.command_on(col.id, "splitv");
                }
            } else {
                conn.command_on(col.id, "splitv");
            }
        }
    }

    refetch(conn, ws_id)
}

fn ensure_single_column(conn: &mut Conn, state: &mut WorkspaceState, ws_id: i64) -> Option<Node> {
    let mut workspace = refetch(conn, ws_id)?;
    while workspace.nodes.len() > 1 {
        let last_id = workspace.nodes.last().unwrap().id;
        let target_id = workspace.nodes[0].id;
        let child_ids: Vec<i64> = {
            let last = find_by_id(&workspace, last_id)?;
            last.nodes.iter().map(|n| n.id).collect()
        };
        for child_id in child_ids {
            move_to_target(conn, state, child_id, target_id);
        }
        workspace = refetch(conn, ws_id)?;
    }
    Some(workspace)
}

fn reflow(conn: &mut Conn, state: &mut WorkspaceState, workspace: &Node) -> bool {
    let lvs: Vec<&Node> = leaves(workspace)
        .into_iter()
        .filter(|l| !is_floating(l))
        .collect();

    if lvs.len() <= 1 {
        return false;
    }

    if state.n_lcol == 0 || lvs.len() <= state.n_lcol {
        if workspace.nodes.len() > 1 {
            ensure_single_column(conn, state, workspace.id);
            return true;
        }
        return false;
    }

    // Need 2 columns.
    let workspace = match ensure_two_columns(conn, state, workspace.id) {
        Some(ws) => ws,
        None => return false,
    };

    if workspace.nodes.len() != 2 {
        return false;
    }

    let lcol = &workspace.nodes[0];
    let rcol = &workspace.nodes[1];

    // Save rcol width.
    state.last_rcol_width = Some(rcol.rect.width);

    // Balance: move windows between columns.
    if lcol.nodes.len() < state.n_lcol && !rcol.nodes.is_empty() {
        let move_id = rcol.nodes[0].id;
        let target_id = lcol.id;
        move_to_target(conn, state, move_id, target_id);
        return true;
    }

    if lcol.nodes.len() > state.n_lcol && lcol.nodes.len() > 1 {
        let move_id = lcol.nodes.last().unwrap().id;
        let rcol_clone = rcol.clone();
        add_to_front(conn, state, &rcol_clone, move_id);
        return true;
    }

    false
}

fn do_reflow(conn: &mut Conn, state: &mut WorkspaceState) {
    for _ in 0..20 {
        let workspace = match refetch(conn, state.workspace_id) {
            Some(ws) => ws,
            None => return,
        };
        if !reflow(conn, state, &workspace) {
            break;
        }
    }
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

fn on_window_new(
    conn: &mut Conn,
    workspaces: &mut HashMap<i64, WorkspaceState>,
    container_id: i64,
) {
    let workspace = get_workspace_of_event(conn, container_id)
        .or_else(|| get_focused_workspace(conn));
    let workspace = match workspace {
        Some(ws) => ws,
        None => return,
    };
    let ws_id = workspace.id;

    // Ensure state exists before entering the main logic.
    {
        let ws_ref = refetch(conn, ws_id).unwrap_or(workspace.clone());
        get_state(workspaces, &ws_ref);
    }

    // Re-fetch to handle the dialog/floating race.
    let workspace = match refetch(conn, ws_id) {
        Some(ws) => ws,
        None => return,
    };

    let old_leaf_ids: Vec<i64> = {
        let state = workspaces.get(&ws_id).unwrap();
        match &state.snapshot {
            Some(snap) => leaves(snap).iter().map(|l| l.id).collect(),
            None => Vec::new(),
        }
    };
    let leaf_ids: Vec<i64> = leaves(&workspace).iter().map(|l| l.id).collect();

    let mut should_reflow = false;
    let mut post_fullscreen_ids: Vec<i64> = Vec::new();

    if old_leaf_ids != leaf_ids {
        // Swap new window with prev so it takes the "current" position.
        if let Some(new_window) = find_by_id(&workspace, container_id) {
            let new_fs = new_window.fullscreen_mode == Some(1);
            if let Some(prev_id) = find_offset_window(&workspace, container_id, -1) {
                conn.command_on(
                    container_id,
                    &format!("swap container with con_id {}", prev_id),
                );
                conn.command_on(container_id, "focus");
                if new_fs {
                    conn.command_on(prev_id, "fullscreen");
                }
            }
        }
        should_reflow = true;
    }

    // Handle fullscreen new windows.
    if let Some(con) = find_by_id(&workspace, container_id) {
        if con.fullscreen_mode == Some(1) {
            post_fullscreen_ids.push(container_id);
        }
    }

    if should_reflow {
        let state = workspaces.get_mut(&ws_id).unwrap();
        do_reflow(conn, state);

        let workspace = refetch(conn, ws_id);
        let focused_ws = get_focused_workspace(conn);
        if let (Some(ws), Some(fws)) = (&workspace, &focused_ws) {
            if ws.id == fws.id {
                if let Some(focused) = find_focused(ws) {
                    let fid = focused.id;
                    let fs = focused.fullscreen_mode == Some(1);
                    refocus_window(conn, fid, fs);
                }
            }
        }
    }

    for fid in post_fullscreen_ids {
        conn.command_on(fid, "focus");
        conn.command_on(fid, "fullscreen");
    }

    let snapshot = refetch(conn, ws_id);
    let state = workspaces.get_mut(&ws_id).unwrap();
    state.snapshot = snapshot;
}

fn on_window_close(
    conn: &mut Conn,
    workspaces: &mut HashMap<i64, WorkspaceState>,
    container_id: i64,
) {
    let workspace = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };
    let ws_id = workspace.id;

    // Ensure state exists.
    {
        let ws_ref = refetch(conn, ws_id).unwrap_or(workspace.clone());
        get_state(workspaces, &ws_ref);
    }

    // Clear zoom state if the zoomed window or its neighbor was closed.
    {
        let state = workspaces.get_mut(&ws_id).unwrap();
        if state.zoomed_id == Some(container_id) {
            state.zoomed_id = None;
            state.zoom_neighbor_id = None;
        }
        if state.zoom_neighbor_id == Some(container_id) {
            state.zoom_neighbor_id = None;
        }
    }

    let old_leaf_ids: Vec<i64> = {
        let state = workspaces.get(&ws_id).unwrap();
        match &state.snapshot {
            Some(snap) => leaves(snap).iter().map(|l| l.id).collect(),
            None => Vec::new(),
        }
    };
    let leaf_ids: Vec<i64> = leaves(&workspace).iter().map(|l| l.id).collect();

    // Find the closed window in the snapshot.
    let closed = {
        let state = workspaces.get(&ws_id).unwrap();
        state
            .snapshot
            .as_ref()
            .and_then(|snap| find_by_id(snap, container_id))
            .cloned()
    };

    let mut should_reflow = false;
    let mut post_fullscreen_target: Option<i64> = None;

    let focused_ws = get_focused_workspace(conn);
    if old_leaf_ids != leaf_ids {
        if let Some(fws) = &focused_ws {
            if fws.id == ws_id {
                if let Some(ref closed_node) = closed {
                    if !is_floating(closed_node) {
                        should_reflow = true;

                        // Focus the "next" window instead of sway's default.
                        let was_fullscreen = closed_node.fullscreen_mode == Some(1);
                        let state = workspaces.get(&ws_id).unwrap();
                        if let Some(ref snap) = state.snapshot {
                            let old_leaves = leaves(snap);
                            let old_ids: Vec<i64> =
                                old_leaves.iter().map(|l| l.id).collect();
                            if let Some(idx) =
                                old_ids.iter().position(|&id| id == container_id)
                            {
                                for offset in 1..=old_ids.len() {
                                    let cand_idx = (idx + offset) % old_ids.len();
                                    let cand_id = old_ids[cand_idx];
                                    if leaf_ids.contains(&cand_id) {
                                        conn.command_on(cand_id, "focus");
                                        if was_fullscreen {
                                            post_fullscreen_target = Some(cand_id);
                                        }
                                        break;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if should_reflow {
        let state = workspaces.get_mut(&ws_id).unwrap();
        do_reflow(conn, state);

        let workspace = refetch(conn, ws_id);
        let focused_ws = get_focused_workspace(conn);
        if let (Some(ws), Some(fws)) = (&workspace, &focused_ws) {
            if ws.id == fws.id {
                if let Some(focused) = find_focused(ws) {
                    let fid = focused.id;
                    let fs = focused.fullscreen_mode == Some(1);
                    refocus_window(conn, fid, fs);
                }
            }
        }
    }

    if let Some(fid) = post_fullscreen_target {
        conn.command_on(fid, "fullscreen");
    }

    // Clean up state for empty workspaces.
    let workspace = refetch(conn, ws_id);
    let is_empty = workspace
        .as_ref()
        .map(|ws| leaves(ws).is_empty())
        .unwrap_or(false);
    if is_empty {
        workspaces.remove(&ws_id);
    } else if let Some(state) = workspaces.get_mut(&ws_id) {
        state.snapshot = workspace;
    }
}

fn reflow_old_workspace(conn: &mut Conn, workspaces: &mut HashMap<i64, WorkspaceState>, new_ws_id: i64) {
    let old_ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };

    let old_ws_id = if old_ws.id == new_ws_id {
        // For cross-output moves, focused workspace may be the same as new.
        conn.command("workspace back_and_forth");
        let back_ws = get_focused_workspace(conn);
        conn.command("workspace back_and_forth");
        match back_ws {
            Some(ws) => ws.id,
            None => return,
        }
    } else {
        old_ws.id
    };

    // Ensure state exists for old workspace.
    if let Some(old_ws_node) = refetch(conn, old_ws_id) {
        get_state(workspaces, &old_ws_node);
        let state = workspaces.get_mut(&old_ws_id).unwrap();
        do_reflow(conn, state);
        state.snapshot = refetch(conn, old_ws_id);
    }
}

fn on_window_move(
    conn: &mut Conn,
    workspaces: &mut HashMap<i64, WorkspaceState>,
    container_id: i64,
) {
    let workspace = get_workspace_of_event(conn, container_id)
        .or_else(|| get_focused_workspace(conn));
    let workspace = match workspace {
        Some(ws) => ws,
        None => return,
    };
    let ws_id = workspace.id;

    // Ensure state exists.
    {
        let ws_ref = refetch(conn, ws_id).unwrap_or(workspace.clone());
        get_state(workspaces, &ws_ref);
    }

    {
        let state = workspaces.get_mut(&ws_id).unwrap();
        if state.pending_moves > 0 {
            debug!(
                "Suppressing self-triggered move ({} pending).",
                state.pending_moves
            );
            state.pending_moves -= 1;
            return;
        }
    }

    // Swap moved window with prev to take its new position.
    if find_by_id(&workspace, container_id).is_some() {
        if let Some(prev_id) = find_offset_window(&workspace, container_id, -1) {
            conn.command_on(
                container_id,
                &format!("swap container with con_id {}", prev_id),
            );
        }
    }

    // Reflow the old workspace (the one the window came from).
    reflow_old_workspace(conn, workspaces, ws_id);

    {
        let state = workspaces.get_mut(&ws_id).unwrap();
        do_reflow(conn, state);
    }

    // Refocus the current workspace (split commands can steal focus).
    if let Some(focused_ws) = get_focused_workspace(conn) {
        if let Some(ref name) = focused_ws.name {
            conn.command(&format!("workspace {}", name));
        }
    }

    let snapshot = refetch(conn, ws_id);
    let state = workspaces.get_mut(&ws_id).unwrap();
    state.snapshot = snapshot;
}

// ---------------------------------------------------------------------------
// Command handlers (nop dispatch)
// ---------------------------------------------------------------------------

fn cmd_promote(conn: &mut Conn, _workspaces: &mut HashMap<i64, WorkspaceState>) {
    promote_window(conn);
}

fn cmd_focus_next(conn: &mut Conn, _workspaces: &mut HashMap<i64, WorkspaceState>) {
    focus_window(conn, 1, None);
}

fn cmd_focus_prev(conn: &mut Conn, _workspaces: &mut HashMap<i64, WorkspaceState>) {
    focus_window(conn, -1, None);
}

fn cmd_swap_next(conn: &mut Conn, _workspaces: &mut HashMap<i64, WorkspaceState>) {
    swap_with_offset(conn, 1, None, true);
}

fn cmd_swap_prev(conn: &mut Conn, _workspaces: &mut HashMap<i64, WorkspaceState>) {
    swap_with_offset(conn, -1, None, true);
}

fn adjust_n_lcol(conn: &mut Conn, workspaces: &mut HashMap<i64, WorkspaceState>, delta: i32) {
    let ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };
    let ws_id = ws.id;

    get_state(workspaces, &ws);

    let focused_id = get_focused_window(conn).map(|w| w.id);

    let n_leaves = leaves(&ws).iter().filter(|l| !is_floating(l)).count();

    let state = workspaces.get_mut(&ws_id).unwrap();
    let effective = state.n_lcol.min(n_leaves);
    let new_val = (effective as i32 + delta).max(0).min(n_leaves as i32) as usize;
    state.n_lcol = new_val;
    debug!("adjust_n_lcol: n_lcol={}", state.n_lcol);

    do_reflow(conn, state);

    if let Some(fid) = focused_id {
        conn.command_on(fid, "focus");
    }

    let snapshot = refetch(conn, ws_id);
    let state = workspaces.get_mut(&ws_id).unwrap();
    state.snapshot = snapshot;
}

fn cmd_flow_left(conn: &mut Conn, workspaces: &mut HashMap<i64, WorkspaceState>) {
    adjust_n_lcol(conn, workspaces, 1);
}

fn cmd_flow_right(conn: &mut Conn, workspaces: &mut HashMap<i64, WorkspaceState>) {
    adjust_n_lcol(conn, workspaces, -1);
}

fn cmd_move_divider(
    conn: &mut Conn,
    workspaces: &mut HashMap<i64, WorkspaceState>,
    args: &[&str],
) {
    let direction = match args.first() {
        Some(d) => *d,
        None => return,
    };
    let amount = if args.len() > 1 { args[1] } else { "50px" };

    let ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };
    if ws.nodes.len() < 2 {
        return;
    }
    let ws_id = ws.id;

    get_state(workspaces, &ws);

    let lcol_id = ws.nodes[0].id;
    match direction {
        "right" => conn.command_on(lcol_id, &format!("resize grow width {}", amount)),
        "left" => conn.command_on(lcol_id, &format!("resize shrink width {}", amount)),
        _ => return,
    }

    if let Some(ws) = refetch(conn, ws_id) {
        if ws.nodes.len() >= 2 {
            let state = workspaces.get_mut(&ws_id).unwrap();
            state.last_rcol_width = Some(ws.nodes[1].rect.width);
        }
    }
}

fn cmd_zoom(conn: &mut Conn, workspaces: &mut HashMap<i64, WorkspaceState>) {
    let ws = match get_focused_workspace(conn) {
        Some(ws) => ws,
        None => return,
    };
    let ws_id = ws.id;

    get_state(workspaces, &ws);

    let focused = match get_focused_window(conn) {
        Some(w) => w,
        None => return,
    };

    let zoomed_id = workspaces.get(&ws_id).unwrap().zoomed_id;

    // Toggle off: unzoom
    if let Some(zid) = zoomed_id {
        if let Some(_zoomed) = refetch(conn, zid) {
            conn.command_on(zid, "floating disable, border pixel 2");

            let state = workspaces.get_mut(&ws_id).unwrap();
            do_reflow(conn, state);

            // Restore position next to saved neighbor.
            let neighbor_id = workspaces.get(&ws_id).unwrap().zoom_neighbor_id;
            if let Some(nid) = neighbor_id {
                if let Some(ws) = refetch(conn, ws_id) {
                    let leaf_ids: Vec<i64> = leaves(&ws)
                        .iter()
                        .filter(|l| !is_floating(l))
                        .map(|l| l.id)
                        .collect();
                    if leaf_ids.contains(&nid) {
                        if refetch(conn, zid).is_some() {
                            let state = workspaces.get_mut(&ws_id).unwrap();
                            move_before(conn, state, zid, nid);
                            do_reflow(conn, state);
                        }
                    }
                }
            }

            if refetch(conn, zid).is_some() {
                conn.command_on(zid, "focus");
            }
        }

        let state = workspaces.get_mut(&ws_id).unwrap();
        state.zoomed_id = None;
        state.zoom_neighbor_id = None;
        state.snapshot = refetch(conn, ws_id);
        return;
    }

    // Toggle on: zoom
    let focused_id = focused.id;

    // Find next window in tiling order for position restore (without wrapping).
    let lvs = leaves(&ws);
    let leaf_ids: Vec<i64> = lvs.iter().map(|l| l.id).collect();
    let zoom_neighbor = leaf_ids
        .iter()
        .position(|&id| id == focused_id)
        .and_then(|idx| {
            if idx + 1 < leaf_ids.len() {
                Some(leaf_ids[idx + 1])
            } else {
                None
            }
        });

    let state = workspaces.get_mut(&ws_id).unwrap();
    state.zoomed_id = Some(focused_id);
    state.zoom_neighbor_id = zoom_neighbor;

    // Float and fill workspace rect.
    let r = ws.rect;
    conn.command_on(
        focused_id,
        &format!(
            "floating enable, border none, resize set {} px {} px, move absolute position {} px {} px",
            r.width, r.height, r.x, r.y
        ),
    );

    do_reflow(conn, state);
    state.snapshot = refetch(conn, ws_id);
}

// ---------------------------------------------------------------------------
// Binding dispatch
// ---------------------------------------------------------------------------

fn on_binding(
    conn: &mut Conn,
    workspaces: &mut HashMap<i64, WorkspaceState>,
    command: &str,
) {
    let parts: Vec<&str> = command.split_whitespace().collect();
    if parts.is_empty() || parts[0] != "nop" {
        return;
    }
    let cmd_name = match parts.get(1) {
        Some(name) => *name,
        None => return,
    };

    conn.enable_buffering();

    match cmd_name {
        "promote_window" => cmd_promote(conn, workspaces),
        "focus_next_window" => cmd_focus_next(conn, workspaces),
        "focus_prev_window" => cmd_focus_prev(conn, workspaces),
        "swap_with_next_window" => cmd_swap_next(conn, workspaces),
        "swap_with_prev_window" => cmd_swap_prev(conn, workspaces),
        "flow_left" => cmd_flow_left(conn, workspaces),
        "flow_right" => cmd_flow_right(conn, workspaces),
        "fullscreen" => cmd_zoom(conn, workspaces),
        "move_divider" => cmd_move_divider(conn, workspaces, &parts[2..]),
        _ => {
            debug!("Unknown nop command: {}", cmd_name);
        }
    }

    conn.disable_buffering();
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() {
    let args = Args::parse();

    // Set up logging.
    let log_level = if args.verbose > 0 { "debug" } else { "warn" };

    if let Some(ref log_file) = args.log_file {
        // Log to file.
        let target = Box::new(
            std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_file)
                .expect("Failed to open log file"),
        );
        env_logger::Builder::new()
            .filter_level(log_level.parse().unwrap())
            .target(env_logger::Target::Pipe(target))
            .format_timestamp_secs()
            .init();
    } else {
        env_logger::Builder::new()
            .filter_level(log_level.parse().unwrap())
            .format_timestamp_secs()
            .init();
    }

    let delay = args.delay;

    // We need two connections:
    // - One for subscribing to events (subscribe consumes self).
    // - One for sending commands / querying tree.
    let event_conn = Connection::new().expect("Failed to connect to sway IPC (events)");
    let event_stream = event_conn
        .subscribe(&[EventType::Binding, EventType::Window])
        .expect("Failed to subscribe to events");

    let mut conn = Conn::new(delay);
    let mut workspaces: HashMap<i64, WorkspaceState> = HashMap::new();

    debug!("sway-xmtall started, listening for events.");

    for event_result in event_stream {
        let event = match event_result {
            Ok(e) => e,
            Err(e) => {
                warn!("Event stream error: {}", e);
                continue;
            }
        };

        match event {
            Event::Binding(binding_event) => {
                let command = binding_event.binding.command.clone();
                if let Err(e) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    on_binding(&mut conn, &mut workspaces, &command);
                })) {
                    warn!("Panic in on_binding: {:?}", e);
                }
            }
            Event::Window(window_event) => {
                let container_id = window_event.container.id;
                let change = window_event.change;
                conn.enable_buffering();
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    match change {
                        swayipc::WindowChange::New => {
                            on_window_new(&mut conn, &mut workspaces, container_id);
                        }
                        swayipc::WindowChange::Close => {
                            on_window_close(&mut conn, &mut workspaces, container_id);
                        }
                        swayipc::WindowChange::Move => {
                            on_window_move(&mut conn, &mut workspaces, container_id);
                        }
                        _ => {}
                    }
                }));
                if let Err(e) = result {
                    warn!("Panic in window event handler: {:?}", e);
                }
                conn.disable_buffering();
            }
            _ => {}
        }
    }
}
