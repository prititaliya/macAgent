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
    /// Absolute time when the overlay should hide (nil = not counting).
    private var hideDeadline: Date?
    private var tickTimer: Timer?
    private var resignObserver: NSObjectProtocol?
    private var activateObserver: NSObjectProtocol?

    init(model: AgentModel) {
        self.model = model
        model.onEvent = { [weak self] in
            self?.show()
        }
        model.onUserActivity = { [weak self] in
            self?.bumpIdleTimer()
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
        bumpIdleTimer()
    }

    func hide() {
        stopIdleTimer()
        clearCountdownUI()
        panel?.orderOut(nil)
    }

    /// Full reset after real activity (type, send, answer, prefs change).
    func bumpIdleTimer() {
        let seconds = OverlayAutoHide.seconds
        guard seconds > 0 else {
            stopIdleTimer()
            clearCountdownUI()
            return
        }
        hideDeadline = Date().addingTimeInterval(TimeInterval(seconds))
        model.hideCountdown = seconds
        model.hideUrgency = false
        model.hidePulse = false
        startTickTimer()
    }

    private func clearCountdownUI() {
        model.hideCountdown = nil
        model.hideUrgency = false
        model.hidePulse = false
    }

    private func stopIdleTimer() {
        tickTimer?.invalidate()
        tickTimer = nil
        hideDeadline = nil
    }

    private func startTickTimer() {
        tickTimer?.invalidate()
        let timer = Timer(timeInterval: 0.2, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.tickIdle()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        tickTimer = timer
    }

    private func tickIdle() {
        guard let panel, panel.isVisible else {
            stopIdleTimer()
            clearCountdownUI()
            return
        }

        let configured = OverlayAutoHide.seconds
        guard configured > 0 else {
            stopIdleTimer()
            clearCountdownUI()
            return
        }

        // Always re-sample mouse — enter/exit events are unreliable on borderless panels.
        let mouseOver = panel.frame.contains(NSEvent.mouseLocation)
        let paused = model.busy || model.pendingConfirm != nil || mouseOver

        if hideDeadline == nil {
            // Out of focus / mouse left while we had no deadline — start fresh.
            hideDeadline = Date().addingTimeInterval(TimeInterval(configured))
        }

        guard let deadline = hideDeadline else { return }

        if paused {
            // Freeze remaining time (do not heartbeat while paused).
            let remaining = max(1, Int(ceil(deadline.timeIntervalSinceNow)))
            hideDeadline = Date().addingTimeInterval(TimeInterval(remaining))
            model.hideCountdown = remaining
            model.hideUrgency = false
            model.hidePulse = false
            return
        }

        let left = max(0, Int(ceil(deadline.timeIntervalSinceNow)))
        model.hideCountdown = left

        // Heartbeat ONLY in the final 3 seconds.
        if left > 0 && left <= 3 {
            model.hideUrgency = true
            // ~2Hz pulse from wall clock — no sticky forever animation.
            model.hidePulse = Int(Date().timeIntervalSince1970 * 2.2) % 2 == 0
        } else {
            model.hideUrgency = false
            model.hidePulse = false
        }

        if left <= 0 {
            hide()
        }
    }

    private func setupPanel() {
        let saved = UserDefaults.standard.string(forKey: "overlayFrame")
        var rect = NSRect(x: 0, y: 0, width: 560, height: 480)
        if let saved {
            let r = NSRectFromString(saved)
            if r.width > 100, r.height > 100 {
                rect = r
            }
        }

        let panel = KeyablePanel(
            contentRect: rect,
            styleMask: [.borderless, .fullSizeContentView, .resizable],
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
        panel.minSize = NSSize(width: 420, height: 300)
        panel.maxSize = NSSize(width: 1200, height: 900)
        panel.setContentSize(rect.size)

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
                self?.bumpIdleTimer()
            }
        )
        let host = NSHostingView(rootView: root)
        host.frame = NSRect(origin: .zero, size: rect.size)
        host.autoresizingMask = [.width, .height]
        panel.contentView = host
        self.panel = panel

        NotificationCenter.default.addObserver(
            forName: NSWindow.didResizeNotification,
            object: panel,
            queue: .main
        ) { [weak panel] _ in
            guard let panel else { return }
            UserDefaults.standard.set(NSStringFromRect(panel.frame), forKey: "overlayFrame")
        }

        // Clicking away (lose key) → resume idle countdown immediately.
        resignObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didResignKeyNotification,
            object: panel,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.tickIdle()
            }
        }
        activateObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.tickIdle()
            }
        }
    }

    private func position(_ panel: NSPanel) {
        if let saved = UserDefaults.standard.string(forKey: "overlayFrame") {
            var frame = NSRectFromString(saved)
            if frame.width >= 100, frame.height >= 100,
               let screen = NSScreen.main {
                let vis = screen.visibleFrame
                frame.size.width = min(max(frame.width, 420), vis.width)
                frame.size.height = min(max(frame.height, 300), vis.height)
                frame.origin.x = min(max(frame.origin.x, vis.minX), vis.maxX - frame.width)
                frame.origin.y = min(max(frame.origin.y, vis.minY), vis.maxY - frame.height)
                panel.setFrame(frame, display: false)
                UserDefaults.standard.set(NSStringFromRect(frame), forKey: "overlayFrame")
            }
            return
        }
        guard let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let size = panel.frame.size
        let x = frame.midX - size.width / 2
        let y = frame.midY - size.height / 2 + 40
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }
}
