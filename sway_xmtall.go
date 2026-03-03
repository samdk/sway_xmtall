// sway-xmtall: an xmonad-like auto-tiler for sway.
// Implements a 'tall' layout: primary column on the left,
// secondary column on the right.
//
// This is a faithful Go port of sway_xmtall.py.
package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"strings"
	"sync"
	"time"

	"go.i3wm.org/i3/v4"
)

// ---------------------------------------------------------------------------
// Per-workspace state
// ---------------------------------------------------------------------------

type WorkspaceState struct {
	WorkspaceID   i3.NodeID
	NLcol         int
	PendingMoves  int
	Snapshot      *i3.Node
	LastRcolWidth *int64
	ZoomedID      *i3.NodeID
	ZoomNeighID   *i3.NodeID
}

func newWorkspaceState(id i3.NodeID) *WorkspaceState {
	return &WorkspaceState{
		WorkspaceID: id,
		NLcol:       1,
	}
}

var (
	workspaces   = make(map[i3.NodeID]*WorkspaceState)
	workspacesMu sync.Mutex
)

func getState(conn *Connection, workspace *i3.Node) *WorkspaceState {
	workspacesMu.Lock()
	defer workspacesMu.Unlock()

	if st, ok := workspaces[workspace.ID]; ok {
		return st
	}

	state := newWorkspaceState(workspace.ID)

	// Infer n_lcol from existing tree (handles config reload).
	if len(workspace.Nodes) > 0 && len(workspace.Nodes[0].Nodes) > 0 {
		state.NLcol = len(workspace.Nodes[0].Nodes)
	}
	if len(workspace.Nodes) >= 2 {
		w := workspace.Nodes[1].Rect.Width
		state.LastRcolWidth = &w
	}

	workspaces[workspace.ID] = state
	log.Printf("Created WorkspaceState(%d, n_lcol=%d) for workspace %d.",
		state.WorkspaceID, state.NLcol, workspace.ID)
	return state
}

// ---------------------------------------------------------------------------
// Tree helpers (the go-i3 library has FindFocused/FindChild on Node, but we
// need additional helpers: findByID, leaves, workspace, etc.)
// ---------------------------------------------------------------------------

// findByID searches the tree rooted at n for a node with the given ID.
func findByID(n *i3.Node, id i3.NodeID) *i3.Node {
	if n == nil {
		return nil
	}
	if n.ID == id {
		return n
	}
	for _, child := range n.Nodes {
		if found := findByID(child, id); found != nil {
			return found
		}
	}
	for _, child := range n.FloatingNodes {
		if found := findByID(child, id); found != nil {
			return found
		}
	}
	return nil
}

// leaves returns all leaf nodes (windows) under n in DFS order.
func leaves(n *i3.Node) []*i3.Node {
	if n == nil {
		return nil
	}
	hasChildren := len(n.Nodes) > 0 || len(n.FloatingNodes) > 0
	if !hasChildren {
		return []*i3.Node{n}
	}
	var result []*i3.Node
	for _, child := range n.Nodes {
		result = append(result, leaves(child)...)
	}
	for _, child := range n.FloatingNodes {
		result = append(result, leaves(child)...)
	}
	return result
}

// findWorkspace returns the workspace node that contains n.
func findWorkspace(root *i3.Node, n *i3.Node) *i3.Node {
	if n == nil || root == nil {
		return nil
	}
	return findWorkspaceOf(root, n.ID)
}

// findWorkspaceOf walks the tree to find which workspace contains the given ID.
func findWorkspaceOf(n *i3.Node, id i3.NodeID) *i3.Node {
	if n == nil {
		return nil
	}
	if n.Type == i3.WorkspaceNode {
		if findByID(n, id) != nil {
			return n
		}
		return nil
	}
	for _, child := range n.Nodes {
		if ws := findWorkspaceOf(child, id); ws != nil {
			return ws
		}
	}
	for _, child := range n.FloatingNodes {
		if ws := findWorkspaceOf(child, id); ws != nil {
			return ws
		}
	}
	return nil
}

// findFocused walks the focus chain of a node to find the deepest focused leaf.
func findFocused(n *i3.Node) *i3.Node {
	if n == nil {
		return nil
	}
	// The node's Focus slice lists child IDs in focus order.
	if len(n.Focus) == 0 {
		return n
	}
	for _, fid := range n.Focus {
		for _, child := range n.Nodes {
			if child.ID == fid {
				return findFocused(child)
			}
		}
		for _, child := range n.FloatingNodes {
			if child.ID == fid {
				return findFocused(child)
			}
		}
	}
	// Fallback: return self if no focused child found.
	return n
}

// isFloating checks if a container is floating.
func isFloating(c *i3.Node) bool {
	return c.Floating == "user_on" || c.Floating == "auto_on" || c.Type == i3.FloatingCon
}

// ---------------------------------------------------------------------------
// Connection with command buffering
// ---------------------------------------------------------------------------

type Connection struct {
	mu             sync.Mutex
	buffering      bool
	commandBuffer  []string
	delayDuration  time.Duration
}

func newConnection(delay time.Duration) *Connection {
	return &Connection{
		delayDuration: delay,
	}
}

// command sends or buffers a command string.
func (c *Connection) command(payload string) {
	c.mu.Lock()
	if c.buffering {
		log.Printf("Buffering: %s", payload)
		c.commandBuffer = append(c.commandBuffer, payload)
		c.mu.Unlock()
		return
	}
	c.mu.Unlock()

	log.Printf("Executing: %s", payload)
	if c.delayDuration > 0 {
		time.Sleep(c.delayDuration)
	}
	if _, err := i3.RunCommand(payload); err != nil && !i3.IsUnsuccessful(err) {
		log.Printf("Command error: %v", err)
	}
}

// nodeCommand sends a command targeted at a specific node.
func (c *Connection) nodeCommand(node *i3.Node, cmd string) {
	c.command(fmt.Sprintf("[con_id=%d] %s", node.ID, cmd))
}

// enableBuffering turns on command buffering.
func (c *Connection) enableBuffering() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.buffering = true
}

// disableBuffering flushes the buffer and turns off buffering.
func (c *Connection) disableBuffering() {
	c.mu.Lock()
	if !c.buffering {
		c.mu.Unlock()
		return
	}
	c.buffering = false
	if len(c.commandBuffer) == 0 {
		c.mu.Unlock()
		return
	}
	payload := strings.Join(c.commandBuffer, ";")
	c.commandBuffer = nil
	c.mu.Unlock()

	c.command(payload)
}

// getTree flushes the buffer, fetches the tree, then re-enables buffering.
func (c *Connection) getTree() *i3.Node {
	c.disableBuffering()
	tree, err := i3.GetTree()
	c.enableBuffering()
	if err != nil {
		log.Printf("GetTree error: %v", err)
		return nil
	}
	return tree.Root
}

// getWorkspaces flushes the buffer, fetches workspaces, then re-enables buffering.
func (c *Connection) getWorkspaces() []i3.Workspace {
	c.disableBuffering()
	ws, err := i3.GetWorkspaces()
	c.enableBuffering()
	if err != nil {
		log.Printf("GetWorkspaces error: %v", err)
		return nil
	}
	return ws
}

// ---------------------------------------------------------------------------
// IPC helpers
// ---------------------------------------------------------------------------

func getFocusedWorkspace(conn *Connection) *i3.Node {
	for _, reply := range conn.getWorkspaces() {
		if reply.Focused {
			root := conn.getTree()
			if root == nil {
				return nil
			}
			node := findByID(root, i3.NodeID(reply.ID))
			if node != nil {
				return findWorkspace(root, node)
			}
			return nil
		}
	}
	return nil
}

func getFocusedWindow(conn *Connection) *i3.Node {
	ws := getFocusedWorkspace(conn)
	if ws == nil {
		return nil
	}
	return findFocused(ws)
}

func getWorkspaceOfEvent(conn *Connection, containerID i3.NodeID) *i3.Node {
	root := conn.getTree()
	if root == nil {
		return nil
	}
	window := findByID(root, containerID)
	if window == nil {
		return nil
	}
	return findWorkspaceOf(root, containerID)
}

func refetch(conn *Connection, container *i3.Node) *i3.Node {
	if container == nil {
		return nil
	}
	root := conn.getTree()
	if root == nil {
		return nil
	}
	return findByID(root, container.ID)
}

// ---------------------------------------------------------------------------
// Move helpers
// ---------------------------------------------------------------------------

const mark = "__sway_xmtall_mark"

func commandMove(conn *Connection, state *WorkspaceState, node *i3.Node, moveArgs string) {
	state.PendingMoves++
	conn.nodeCommand(node, fmt.Sprintf("move %s", moveArgs))
}

func moveToTarget(conn *Connection, state *WorkspaceState, node *i3.Node, target *i3.Node) {
	conn.nodeCommand(target, fmt.Sprintf("mark --add %s", mark))
	commandMove(conn, state, node, fmt.Sprintf("window to mark %s", mark))
	conn.nodeCommand(target, fmt.Sprintf("unmark %s", mark))
}

func moveBefore(conn *Connection, state *WorkspaceState, node *i3.Node, target *i3.Node) {
	moveToTarget(conn, state, node, target)
	commandMove(conn, state, node, "up")
}

func addToFront(conn *Connection, state *WorkspaceState, column *i3.Node, node *i3.Node) {
	if len(column.Nodes) == 0 {
		moveToTarget(conn, state, node, column)
		return
	}
	moveBefore(conn, state, node, column.Nodes[0])
}

// ---------------------------------------------------------------------------
// Window operations
// ---------------------------------------------------------------------------

func findOffsetWindow(conn *Connection, container *i3.Node, offset int) *i3.Node {
	root := conn.getTree()
	if root == nil {
		return nil
	}
	ws := findWorkspaceOf(root, container.ID)
	if ws == nil {
		return nil
	}
	allLeaves := leaves(ws)
	var ids []i3.NodeID
	for _, l := range allLeaves {
		ids = append(ids, l.ID)
	}
	idx := -1
	for i, id := range ids {
		if id == container.ID {
			idx = i
			break
		}
	}
	if idx < 0 {
		return nil // floating
	}
	n := len(allLeaves)
	target := ((idx + offset) % n + n) % n // Go modulo can be negative
	return allLeaves[target]
}

func focusWindow(conn *Connection, offset int, window *i3.Node) {
	focused := window
	if focused == nil {
		focused = getFocusedWindow(conn)
	}
	if focused == nil {
		return
	}
	target := findOffsetWindow(conn, focused, offset)
	if target != nil {
		conn.nodeCommand(target, "focus")
		if focused.FullscreenMode == 1 {
			conn.nodeCommand(target, "fullscreen")
		}
	}
}

func refocusWindow(conn *Connection, window *i3.Node) {
	// Focusing next then back moves the cursor to the window center,
	// unlike a plain focus which can leave it on the border.
	focusWindow(conn, 1, window)
	conn.nodeCommand(window, "focus")
	if window.FullscreenMode == 1 {
		conn.nodeCommand(window, "fullscreen")
	}
}

func swapWithOffset(conn *Connection, offset int, window *i3.Node, focusAfter bool) {
	focused := window
	if focused == nil {
		focused = getFocusedWindow(conn)
	}
	if focused == nil {
		return
	}
	target := findOffsetWindow(conn, focused, offset)
	if target != nil {
		conn.nodeCommand(focused, fmt.Sprintf("swap container with con_id %d", target.ID))
		if focusAfter {
			conn.nodeCommand(focused, "focus")
			if focused.FullscreenMode == 1 {
				conn.nodeCommand(target, "fullscreen")
			}
		}
	}
}

func promoteWindow(conn *Connection) {
	ws := getFocusedWorkspace(conn)
	focused := getFocusedWindow(conn)
	if ws == nil || focused == nil {
		return
	}
	allLeaves := leaves(ws)
	if len(allLeaves) == 0 {
		return
	}
	// Find largest leaf by area.
	var largest *i3.Node
	var largestArea int64
	for _, l := range allLeaves {
		area := l.Rect.Width * l.Rect.Height
		if area > largestArea {
			largestArea = area
			largest = l
		}
	}
	if largest == nil {
		return
	}
	conn.nodeCommand(focused, fmt.Sprintf("swap container with con_id %d", largest.ID))
	conn.nodeCommand(focused, "focus")
	if focused.FullscreenMode == 1 {
		conn.nodeCommand(focused, "fullscreen")
	}
}

// ---------------------------------------------------------------------------
// Tall layout reflow
// ---------------------------------------------------------------------------

func ensureTwoColumns(conn *Connection, state *WorkspaceState, workspace *i3.Node) *i3.Node {
	// Merge extra columns into column 1.
	for len(workspace.Nodes) > 2 {
		extra := workspace.Nodes[len(workspace.Nodes)-1]
		target := workspace.Nodes[1]
		for _, node := range extra.Nodes {
			moveToTarget(conn, state, node, target)
		}
		workspace = refetch(conn, workspace)
		if workspace == nil {
			return nil
		}
	}

	// Split single column if we have more than n_lcol windows.
	if len(workspace.Nodes) == 1 {
		col := workspace.Nodes[0]
		if len(col.Nodes) > state.NLcol {
			focused := findFocused(workspace)
			commandMove(conn, state, col.Nodes[len(col.Nodes)-1], "right")
			if focused != nil {
				conn.nodeCommand(focused, "focus")
			}
			workspace = refetch(conn, workspace)
			if workspace == nil {
				return nil
			}
		}
	}

	// Ensure both columns are splitv; restore rcol width if recreating.
	for i, col := range workspace.Nodes {
		if col.Layout != i3.SplitV {
			if i == 1 && state.LastRcolWidth != nil {
				conn.nodeCommand(col, fmt.Sprintf("splitv, resize set width %d px", *state.LastRcolWidth))
			} else {
				conn.nodeCommand(col, "splitv")
			}
		}
	}

	return refetch(conn, workspace)
}

func ensureSingleColumn(conn *Connection, state *WorkspaceState, workspace *i3.Node) *i3.Node {
	for len(workspace.Nodes) > 1 {
		last := workspace.Nodes[len(workspace.Nodes)-1]
		target := workspace.Nodes[0]
		for _, node := range last.Nodes {
			moveToTarget(conn, state, node, target)
		}
		workspace = refetch(conn, workspace)
		if workspace == nil {
			return nil
		}
	}
	return workspace
}

func reflow(conn *Connection, state *WorkspaceState, workspace *i3.Node) bool {
	// One pass of structural correction. Returns true if a mutation occurred.
	var nonFloating []*i3.Node
	for _, l := range leaves(workspace) {
		if !isFloating(l) {
			nonFloating = append(nonFloating, l)
		}
	}

	if len(nonFloating) <= 1 {
		return false
	}

	if state.NLcol == 0 || len(nonFloating) <= state.NLcol {
		if len(workspace.Nodes) > 1 {
			ensureSingleColumn(conn, state, workspace)
			return true
		}
		return false
	}

	// Need 2 columns.
	workspace = ensureTwoColumns(conn, state, workspace)
	if workspace == nil {
		return false
	}
	cols := workspace.Nodes

	if len(cols) != 2 {
		return false
	}

	lcol, rcol := cols[0], cols[1]

	// Save rcol width.
	w := rcol.Rect.Width
	state.LastRcolWidth = &w

	// Balance: move windows between columns.
	if len(lcol.Nodes) < state.NLcol && len(rcol.Nodes) > 0 {
		moveToTarget(conn, state, rcol.Nodes[0], lcol)
		return true
	}

	if len(lcol.Nodes) > state.NLcol && len(lcol.Nodes) > 1 {
		addToFront(conn, state, rcol, lcol.Nodes[len(lcol.Nodes)-1])
		return true
	}

	return false
}

func doReflow(conn *Connection, state *WorkspaceState) {
	for range 20 { // safety bound
		root := conn.getTree()
		if root == nil {
			return
		}
		workspace := findByID(root, state.WorkspaceID)
		if workspace == nil {
			return
		}
		if !reflow(conn, state, workspace) {
			break
		}
	}
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

func onWindowNew(conn *Connection, containerID i3.NodeID) {
	workspace := getWorkspaceOfEvent(conn, containerID)
	if workspace == nil {
		workspace = getFocusedWorkspace(conn)
	}
	if workspace == nil {
		return
	}
	state := getState(conn, workspace)

	conn.enableBuffering()
	defer conn.disableBuffering()

	// Re-fetch to handle the dialog/floating race: sway creates dialogs as
	// tiling then immediately floats them.
	workspace = refetch(conn, workspace)
	if workspace == nil {
		return
	}

	oldLeafIDs := make(map[i3.NodeID]bool)
	if state.Snapshot != nil {
		for _, l := range leaves(state.Snapshot) {
			oldLeafIDs[l.ID] = true
		}
	}
	leafIDs := make(map[i3.NodeID]bool)
	for _, l := range leaves(workspace) {
		leafIDs[l.ID] = true
	}

	shouldReflow := false
	type hookFunc func()
	var postHooks []hookFunc

	if !mapsEqual(oldLeafIDs, leafIDs) {
		// Swap new window with prev so it takes the "current" position.
		newWindow := findByID(workspace, containerID)
		if newWindow != nil {
			swapWithOffset(conn, -1, newWindow, true)
		}
		shouldReflow = true
	}

	// Handle fullscreen new windows.
	con := findByID(workspace, containerID)
	if con != nil && con.FullscreenMode == 1 {
		conCopy := con // capture for closure
		postHooks = append(postHooks, func() {
			conn.nodeCommand(conCopy, "focus")
		})
		postHooks = append(postHooks, func() {
			conn.nodeCommand(conCopy, "fullscreen")
		})
	}

	if shouldReflow {
		doReflow(conn, state)

		workspace = refetch(conn, workspace)
		focusedWS := getFocusedWorkspace(conn)
		if workspace != nil && focusedWS != nil && workspace.ID == focusedWS.ID {
			focused := findFocused(workspace)
			if focused != nil {
				refocusWindow(conn, focused)
			}
		}
	}

	for _, hook := range postHooks {
		hook()
	}

	state.Snapshot = refetch(conn, workspace)
}

func onWindowClose(conn *Connection, containerID i3.NodeID) {
	workspace := getFocusedWorkspace(conn)
	if workspace == nil {
		return
	}
	state := getState(conn, workspace)

	conn.enableBuffering()
	defer conn.disableBuffering()

	// Clear zoom state if the zoomed window or its neighbor was closed.
	if state.ZoomedID != nil && *state.ZoomedID == containerID {
		state.ZoomedID = nil
		state.ZoomNeighID = nil
	}
	if state.ZoomNeighID != nil && *state.ZoomNeighID == containerID {
		state.ZoomNeighID = nil
	}

	shouldReflow := false
	type hookFunc func()
	var postHooks []hookFunc

	oldLeafIDs := make(map[i3.NodeID]bool)
	if state.Snapshot != nil {
		for _, l := range leaves(state.Snapshot) {
			oldLeafIDs[l.ID] = true
		}
	}
	leafIDs := make(map[i3.NodeID]bool)
	for _, l := range leaves(workspace) {
		leafIDs[l.ID] = true
	}

	var closed *i3.Node
	if state.Snapshot != nil {
		closed = findByID(state.Snapshot, containerID)
	}

	focusedWS := getFocusedWorkspace(conn)
	if !mapsEqual(oldLeafIDs, leafIDs) &&
		focusedWS != nil && workspace.ID == focusedWS.ID &&
		closed != nil && !isFloating(closed) {
		shouldReflow = true

		// Focus the "next" window instead of sway's default.
		wasFullscreen := closed.FullscreenMode == 1
		oldLeaves := leaves(state.Snapshot)
		var oldIDs []i3.NodeID
		for _, l := range oldLeaves {
			oldIDs = append(oldIDs, l.ID)
		}
		closedIdx := -1
		for i, id := range oldIDs {
			if id == closed.ID {
				closedIdx = i
				break
			}
		}
		if closedIdx >= 0 {
			for offset := 1; offset <= len(oldIDs); offset++ {
				candIdx := (closedIdx + offset) % len(oldIDs)
				candidate := oldLeaves[candIdx]
				if leafIDs[candidate.ID] {
					conn.nodeCommand(candidate, "focus")
					if wasFullscreen {
						candCopy := candidate // capture for closure
						postHooks = append(postHooks, func() {
							conn.nodeCommand(candCopy, "fullscreen")
						})
					}
					break
				}
			}
		}
	}

	if shouldReflow {
		doReflow(conn, state)

		workspace = refetch(conn, workspace)
		focusedWS2 := getFocusedWorkspace(conn)
		if workspace != nil && focusedWS2 != nil && workspace.ID == focusedWS2.ID {
			focused := findFocused(workspace)
			if focused != nil {
				refocusWindow(conn, focused)
			}
		}
	}

	for _, hook := range postHooks {
		hook()
	}

	// Clean up state for empty workspaces.
	workspace = refetch(conn, workspace)
	if workspace != nil && len(leaves(workspace)) == 0 {
		workspacesMu.Lock()
		delete(workspaces, workspace.ID)
		workspacesMu.Unlock()
	} else {
		state.Snapshot = workspace
	}
}

func onWindowMove(conn *Connection, containerID i3.NodeID) {
	workspace := getWorkspaceOfEvent(conn, containerID)
	if workspace == nil {
		workspace = getFocusedWorkspace(conn)
	}
	if workspace == nil {
		return
	}
	state := getState(conn, workspace)

	if state.PendingMoves > 0 {
		log.Printf("Suppressing self-triggered move (%d pending).", state.PendingMoves)
		state.PendingMoves--
		return
	}

	conn.enableBuffering()
	defer conn.disableBuffering()

	// Swap moved window with prev to take its new position.
	window := findByID(workspace, containerID)
	if window != nil {
		swapWithOffset(conn, -1, window, false)
	}

	// Reflow the old workspace (the one the window came from).
	reflowOldWorkspace(conn, workspace)

	doReflow(conn, state)

	// Refocus the current workspace (split commands can steal focus).
	focusedWS := getFocusedWorkspace(conn)
	if focusedWS != nil {
		conn.command(fmt.Sprintf("workspace %s", focusedWS.Name))
	}

	state.Snapshot = refetch(conn, workspace)
}

func reflowOldWorkspace(conn *Connection, newWorkspace *i3.Node) {
	oldWorkspace := getFocusedWorkspace(conn)
	if oldWorkspace == nil {
		return
	}

	// For cross-output moves, focused workspace may be the same as new.
	if oldWorkspace.ID == newWorkspace.ID {
		conn.command("workspace back_and_forth")
		oldWorkspace = getFocusedWorkspace(conn)
		conn.command("workspace back_and_forth")
	}

	if oldWorkspace != nil {
		oldState := getState(conn, oldWorkspace)
		doReflow(conn, oldState)
		oldState.Snapshot = refetch(conn, oldWorkspace)
	}
}

// ---------------------------------------------------------------------------
// Command handlers (dispatched from nop bindings)
// ---------------------------------------------------------------------------

func cmdPromote(conn *Connection, _ []string) {
	promoteWindow(conn)
}

func cmdFocusNext(conn *Connection, _ []string) {
	focusWindow(conn, 1, nil)
}

func cmdFocusPrev(conn *Connection, _ []string) {
	focusWindow(conn, -1, nil)
}

func cmdSwapNext(conn *Connection, _ []string) {
	swapWithOffset(conn, 1, nil, true)
}

func cmdSwapPrev(conn *Connection, _ []string) {
	swapWithOffset(conn, -1, nil, true)
}

func adjustNLcol(conn *Connection, delta int) {
	ws := getFocusedWorkspace(conn)
	if ws == nil {
		return
	}
	state := getState(conn, ws)
	focused := getFocusedWindow(conn)

	var nonFloating []*i3.Node
	for _, l := range leaves(ws) {
		if !isFloating(l) {
			nonFloating = append(nonFloating, l)
		}
	}
	nLeaves := len(nonFloating)

	effective := state.NLcol
	if effective > nLeaves {
		effective = nLeaves
	}
	if effective < 0 {
		effective = 0
	}

	newVal := effective + delta
	if newVal < 0 {
		newVal = 0
	}
	if newVal > nLeaves {
		newVal = nLeaves
	}
	state.NLcol = newVal

	log.Printf("adjust_n_lcol: n_lcol=%d", state.NLcol)
	doReflow(conn, state)

	if focused != nil {
		conn.nodeCommand(focused, "focus")
	}
	state.Snapshot = refetch(conn, ws)
}

func cmdFlowLeft(conn *Connection, _ []string) {
	adjustNLcol(conn, 1)
}

func cmdFlowRight(conn *Connection, _ []string) {
	adjustNLcol(conn, -1)
}

func cmdMoveDivider(conn *Connection, args []string) {
	direction := ""
	amount := "50px"
	if len(args) > 0 {
		direction = args[0]
	}
	if len(args) > 1 {
		amount = args[1]
	}

	ws := getFocusedWorkspace(conn)
	if ws == nil || len(ws.Nodes) < 2 {
		return
	}
	state := getState(conn, ws)
	lcol := ws.Nodes[0]

	if direction == "right" {
		conn.nodeCommand(lcol, fmt.Sprintf("resize grow width %s", amount))
	} else if direction == "left" {
		conn.nodeCommand(lcol, fmt.Sprintf("resize shrink width %s", amount))
	}

	ws = refetch(conn, ws)
	if ws != nil && len(ws.Nodes) >= 2 {
		w := ws.Nodes[1].Rect.Width
		state.LastRcolWidth = &w
	}
}

func cmdZoom(conn *Connection, _ []string) {
	ws := getFocusedWorkspace(conn)
	if ws == nil {
		return
	}
	state := getState(conn, ws)
	focused := getFocusedWindow(conn)
	if focused == nil {
		return
	}

	// Toggle off: unzoom
	if state.ZoomedID != nil {
		root := conn.getTree()
		if root == nil {
			return
		}
		zoomed := findByID(root, *state.ZoomedID)
		if zoomed != nil {
			conn.nodeCommand(zoomed, "floating disable, border pixel 2")
			doReflow(conn, state)

			// Restore position next to saved neighbor.
			if state.ZoomNeighID != nil {
				ws = refetch(conn, ws)
				if ws != nil {
					var leafIDs []i3.NodeID
					for _, l := range leaves(ws) {
						if !isFloating(l) {
							leafIDs = append(leafIDs, l.ID)
						}
					}
					zoomed = refetch(conn, zoomed)
					if zoomed != nil && containsID(leafIDs, *state.ZoomNeighID) {
						root2 := conn.getTree()
						if root2 != nil {
							neighbor := findByID(root2, *state.ZoomNeighID)
							if neighbor != nil {
								moveBefore(conn, state, zoomed, neighbor)
								doReflow(conn, state)
							}
						}
					}
				}
			}
			zoomed = refetch(conn, zoomed)
			if zoomed != nil {
				conn.nodeCommand(zoomed, "focus")
			}
		}
		state.ZoomedID = nil
		state.ZoomNeighID = nil
		state.Snapshot = refetch(conn, ws)
		return
	}

	// Toggle on: zoom
	zid := focused.ID
	state.ZoomedID = &zid

	// Find next window in tiling order for position restore (without wrapping).
	allLeaves := leaves(ws)
	var leafIDs []i3.NodeID
	for _, l := range allLeaves {
		leafIDs = append(leafIDs, l.ID)
	}
	idx := -1
	for i, id := range leafIDs {
		if id == focused.ID {
			idx = i
			break
		}
	}
	if idx >= 0 && idx+1 < len(leafIDs) {
		nid := leafIDs[idx+1]
		state.ZoomNeighID = &nid
	} else {
		state.ZoomNeighID = nil
	}

	// Float and fill workspace rect.
	r := ws.Rect
	conn.nodeCommand(focused, fmt.Sprintf(
		"floating enable, border none, resize set %d px %d px, move absolute position %d px %d px",
		r.Width, r.Height, r.X, r.Y))

	doReflow(conn, state)
	state.Snapshot = refetch(conn, ws)
}

// ---------------------------------------------------------------------------
// Command dispatch table
// ---------------------------------------------------------------------------

type commandHandler func(conn *Connection, args []string)

var commands = map[string]commandHandler{
	"promote_window":       cmdPromote,
	"focus_next_window":    cmdFocusNext,
	"focus_prev_window":    cmdFocusPrev,
	"swap_with_next_window": cmdSwapNext,
	"swap_with_prev_window": cmdSwapPrev,
	"flow_left":            cmdFlowLeft,
	"flow_right":           cmdFlowRight,
	"fullscreen":           cmdZoom,
	"move_divider":         cmdMoveDivider,
}

func onBinding(conn *Connection, ev *i3.BindingEvent) {
	parts := strings.Fields(ev.Binding.Command)
	if len(parts) == 0 || parts[0] != "nop" {
		return
	}
	if len(parts) < 2 {
		return
	}
	cmdName := parts[1]

	handler, ok := commands[cmdName]
	if !ok {
		return
	}

	conn.enableBuffering()
	defer conn.disableBuffering()

	handler(conn, parts[2:])
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

func mapsEqual(a, b map[i3.NodeID]bool) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range a {
		if !b[k] {
			return false
		}
	}
	return true
}

func containsID(ids []i3.NodeID, id i3.NodeID) bool {
	for _, x := range ids {
		if x == id {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	verbose := flag.Bool("verbose", false, "Enable debug logging.")
	logFile := flag.String("log-file", "", "Log file path (default: stderr).")
	delay := flag.Float64("delay", 0.0, "Sleep between commands in seconds (debug).")
	flag.BoolVar(verbose, "v", false, "Enable debug logging (shorthand).")

	flag.Parse()

	// Set up logging.
	if *verbose {
		log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds | log.Lshortfile)
		if *logFile != "" {
			f, err := os.OpenFile(*logFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Cannot open log file: %v\n", err)
				os.Exit(1)
			}
			defer f.Close()
			log.SetOutput(f)
		}
	} else {
		// Suppress log output when not verbose.
		log.SetOutput(io.Discard)
	}

	conn := newConnection(time.Duration(float64(time.Second) * *delay))

	// Subscribe to window and binding events.
	recv := i3.Subscribe(i3.WindowEventType, i3.BindingEventType)

	log.Println("sway-xmtall started, listening for events...")

	for recv.Next() {
		switch ev := recv.Event().(type) {
		case *i3.WindowEvent:
			switch ev.Change {
			case "new":
				func() {
					defer recoverAndLog("on_window_new")
					onWindowNew(conn, ev.Container.ID)
				}()
			case "close":
				func() {
					defer recoverAndLog("on_window_close")
					onWindowClose(conn, ev.Container.ID)
				}()
			case "move":
				func() {
					defer recoverAndLog("on_window_move")
					onWindowMove(conn, ev.Container.ID)
				}()
			}
		case *i3.BindingEvent:
			func() {
				defer recoverAndLog("on_binding")
				onBinding(conn, ev)
			}()
		}
	}

	log.Println("Event receiver closed, exiting.")
}

func recoverAndLog(context string) {
	if r := recover(); r != nil {
		log.Printf("Panic in %s: %v", context, r)
	}
}
