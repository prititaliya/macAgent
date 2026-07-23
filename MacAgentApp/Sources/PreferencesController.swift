import AppKit
import SwiftUI

/// Owns a normal NSWindow for Preferences — more reliable than SwiftUI Settings
/// in LSUIElement / accessory menu-bar apps.
@MainActor
final class PreferencesController {
    static let shared = PreferencesController()

    private var window: NSWindow?

    private init() {}

    func show() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)

        if window == nil {
            let root = PreferencesView()
                .environmentObject(AgentModel.shared)
                .tint(Theme.accent)
                .frame(minWidth: 880, minHeight: 560)

            let hosting = NSHostingController(rootView: root)
            let win = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 920, height: 600),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            win.title = "MacAgent Preferences"
            win.contentViewController = hosting
            win.isReleasedWhenClosed = false
            win.center()
            win.setFrameAutosaveName("MacAgentPreferences")
            window = win
        }

        window?.makeKeyAndOrderFront(nil)
        window?.orderFrontRegardless()
    }
}
