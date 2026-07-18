import AppKit
import SwiftUI

/// Borderless panels refuse key status unless we override this.
final class KeyablePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

@MainActor
final class OverlayController {
    private var panel: KeyablePanel?
    private let model: AgentModel
    private var hideWork: DispatchWorkItem?
    /// True while pointer is over the overlay or user is actively using it.
    private var pointerInside = false
    private var tracking: NSTrackingArea?

    init(model: AgentModel) {
        self.model = model
        model.onEvent = { [weak self] in
            self?.show()
        }
        model.onUserActivity = { [weak self] in
            self?.scheduleAutoHide()
        }
    }

    func toggle() {
        if panel?.isVisible == true {
            hide()
        } else {
            show()
        }
    }

    func show() {
        if panel == nil {
            setupPanel()
        }
        guard let panel else { return }
        position(panel)
        NSApp.setActivationPolicy(.accessory)
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
        scheduleAutoHide()
    }

    func hide() {
        hideWork?.cancel()
        hideWork = nil
        panel?.orderOut(nil)
    }

    /// Restart the disappear timer from Preferences (Never = 0).
    func scheduleAutoHide() {
        hideWork?.cancel()
        hideWork = nil
        let seconds = OverlayAutoHide.seconds
        guard seconds > 0 else { return }

        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            if self.shouldDeferHide() {
                // Still engaged — wait another full interval.
                self.scheduleAutoHide()
                return
            }
            self.hide()
        }
        hideWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(seconds), execute: work)
    }

    /// Don't auto-hide while busy, focused, hovering, or selecting text.
    private func shouldDeferHide() -> Bool {
        if model.busy { return true }
        if pointerInside { return true }
        guard let panel, panel.isVisible else { return false }
        if panel.isKeyWindow { return true }
        // Mouse still over the panel frame (tracking area can miss some cases).
        let mouse = NSEvent.mouseLocation
        if panel.frame.contains(mouse) {
            pointerInside = true
            return true
        }
        return false
    }

    private func setupPanel() {
        let panel = KeyablePanel(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 480),
            styleMask: [.borderless, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.becomesKeyOnlyIfNeeded = false
        panel.acceptsMouseMovedEvents = true

        let root = OverlayView(
            model: model,
            onDismiss: { [weak self] in self?.hide() },
            onPrefs: {
                AppDelegate.shared?.openPrefs()
            },
            onQuit: {
                AppDelegate.shared?.quitApp()
            },
            onInteract: { [weak self] in
                self?.pointerInside = true
                self?.scheduleAutoHide()
            }
        )
        let host = TrackingHostingView(rootView: root)
        host.frame = NSRect(x: 0, y: 0, width: 560, height: 480)
        host.onPointerInside = { [weak self] inside in
            Task { @MainActor in
                self?.pointerInside = inside
                if inside {
                    self?.scheduleAutoHide()
                }
            }
        }
        panel.contentView = host
        self.panel = panel
    }

    private func position(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let size = panel.frame.size
        let x = frame.midX - size.width / 2
        let y = frame.midY - size.height / 2 + 40
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }
}

/// Reports mouse enter/exit so auto-hide pauses while the cursor is over the overlay.
final class TrackingHostingView<Content: View>: NSHostingView<Content> {
    var onPointerInside: ((Bool) -> Void)?
    private var area: NSTrackingArea?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let area {
            removeTrackingArea(area)
        }
        let options: NSTrackingArea.Options = [
            .mouseEnteredAndExited,
            .mouseMoved,
            .activeAlways,
            .inVisibleRect,
        ]
        let tracking = NSTrackingArea(rect: bounds, options: options, owner: self, userInfo: nil)
        addTrackingArea(tracking)
        area = tracking
    }

    override func mouseEntered(with event: NSEvent) {
        onPointerInside?(true)
    }

    override func mouseExited(with event: NSEvent) {
        onPointerInside?(false)
    }

    override func mouseMoved(with event: NSEvent) {
        onPointerInside?(true)
    }

    override func scrollWheel(with event: NSEvent) {
        onPointerInside?(true)
        super.scrollWheel(with: event)
    }
}
