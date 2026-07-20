import SwiftUI
import AppKit

@main
struct MacAgentMain: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        MenuBarExtra {
            Button("Show Agent") {
                AppDelegate.shared?.showOverlay()
            }
            Button("Preferences…") {
                AppDelegate.shared?.openPrefs()
            }
            Divider()
            Button("Quit MacAgent") {
                AppDelegate.shared?.quitApp()
            }
        } label: {
            // Keep this tiny — a full Logo asset in the menu bar looks like a
            // giant circle stuck on the top of the screen.
            Image(systemName: "sparkles")
        }

        Settings {
            PreferencesView()
                .environmentObject(AgentModel.shared)
                .frame(minWidth: 780, minHeight: 520)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var shared: AppDelegate?

    private var overlay: OverlayController?
    private let model = AgentModel.shared

    func applicationDidFinishLaunching(_ notification: Notification) {
        Self.shared = self

        // Become the only MacAgent — terminate older duplicates (Xcode + script launches stack).
        Self.terminateOtherInstances()

        NSApp.setActivationPolicy(.accessory)

        overlay = OverlayController(model: model)
        overlay?.show()

        HotkeyManager.shared.onToggle = { [weak self] in
            self?.overlay?.toggle()
        }
        HotkeyManager.shared.registerDefault()
        UIBridgeServer.shared.start()

        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification,
            object: nil,
            queue: .main
        ) { _ in
            Task { @MainActor in
                UIBridgeServer.shared.stop()
                AgentModel.shared.shutdown()
            }
        }

        Task {
            await model.bootstrap()
        }
    }

    /// Kill every other MacAgent process so only this launch remains.
    private static func terminateOtherInstances() {
        let myPID = ProcessInfo.processInfo.processIdentifier
        let mine = Bundle.main.bundleIdentifier ?? "com.macagent.app"
        for app in NSWorkspace.shared.runningApplications {
            guard app.processIdentifier != myPID else { continue }
            let matchBundle = app.bundleIdentifier == mine
            let matchName = (app.localizedName ?? "") == "MacAgent"
                || (app.bundleURL?.lastPathComponent == "MacAgent.app")
            guard matchBundle || matchName else { continue }
            app.forceTerminate()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showOverlay()
        return true
    }

    func showOverlay() {
        overlay?.show()
    }

    func openPrefs() {
        PreferencesController.shared.show()
    }

    func quitApp() {
        model.shutdown()
        NSApp.terminate(nil)
    }
}
