import AppKit
import Foundation

/// Overlay size persistence + fixed top-right placement.
enum OverlayFrame {
    static let sizeKey = "overlaySize"
    /// Legacy key — cleared on reset; position is no longer persisted.
    static let frameKey = "overlayFrame"
    static let margin: CGFloat = 16
    static let defaultSize = NSSize(width: 560, height: 480)

    /// Top-right of the given screen's visible area (below menu bar).
    static func defaultFrame(on screen: NSScreen, size: NSSize = defaultSize) -> NSRect {
        let vis = screen.visibleFrame
        let width = min(max(size.width, 420), vis.width)
        let height = min(max(size.height, 300), vis.height)
        return NSRect(
            x: vis.maxX - width - margin,
            y: vis.maxY - height - margin,
            width: width,
            height: height
        )
    }

    static func loadSavedSize() -> NSSize? {
        if let saved = UserDefaults.standard.string(forKey: sizeKey) {
            let size = NSSizeFromString(saved)
            if size.width >= 100, size.height >= 100 { return size }
        }
        // Migrate one-time from legacy saved frame (size only).
        if let legacy = UserDefaults.standard.string(forKey: frameKey) {
            let frame = NSRectFromString(legacy)
            if frame.width >= 100, frame.height >= 100 {
                return frame.size
            }
        }
        return nil
    }

    static func saveSize(_ size: NSSize) {
        UserDefaults.standard.set(NSStringFromSize(size), forKey: sizeKey)
    }

    static func resetToDefault() {
        UserDefaults.standard.removeObject(forKey: sizeKey)
        UserDefaults.standard.removeObject(forKey: frameKey)
    }

    /// Always anchor the panel to the top-right of `screen` (main if nil).
    static func apply(to panel: NSPanel, preferredScreen: NSScreen? = nil) {
        let screen = preferredScreen ?? NSScreen.main
        guard let screen else { return }
        let frame = defaultFrame(on: screen, size: panel.frame.size)
        panel.setFrame(frame, display: false)
        saveSize(frame.size)
    }
}

/// User-selectable overlay auto-hide timeout (seconds). 0 = never.
enum OverlayAutoHide {
    static let defaultsKey = "overlayAutoHideSeconds"
    static let choices: [(label: String, seconds: Int)] = [
        ("Never", 0),
        ("5 seconds", 5),
        ("10 seconds", 10),
        ("15 seconds", 15),
        ("30 seconds", 30),
        ("60 seconds", 60),
    ]

    static var seconds: Int {
        get {
            if UserDefaults.standard.object(forKey: defaultsKey) == nil {
                return 15
            }
            return UserDefaults.standard.integer(forKey: defaultsKey)
        }
        set {
            UserDefaults.standard.set(newValue, forKey: defaultsKey)
        }
    }
}
