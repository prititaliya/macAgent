import SwiftUI
import AppKit

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var tab: Tab = .live

    enum Tab: String, CaseIterable, Identifiable {
        case live = "Live"
        case history = "History"
        case sites = "Sites"
        case apps = "Apps"
        case settings = "Settings"
        case debug = "Debug"
        case status = "Status"
        var id: String { rawValue }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Group {
                switch tab {
                case .live:
                    ScrollView {
                        LivePanelView()
                            .padding(20)
                    }
                case .history:
                    HistoryView()
                case .sites:
                    SitesView()
                case .apps:
                    AppsView()
                case .settings:
                    SettingsView()
                case .debug:
                    DebugView()
                case .status:
                    StatusView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            NSApp.setActivationPolicy(.regular)
        }
    }

    private var header: some View {
        HStack(spacing: 16) {
            Text("MacAgent")
                .font(.system(size: 18, weight: .semibold, design: .rounded))
            Picker("", selection: $tab) {
                ForEach(Tab.allCases) { t in
                    Text(t.rawValue).tag(t)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 360)

            Spacer()

            Circle()
                .fill(model.connected ? Color.green.opacity(0.9) : Color.orange.opacity(0.9))
                .frame(width: 9, height: 9)
            Text(model.connected ? "Connected" : (model.daemonStarting ? "Starting…" : "Offline"))
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(.secondary)

            Button {
                Task { await model.refreshAll() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Refresh")
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }
}

/// Ensures we stop an app-owned daemon on quit.
@MainActor
final class QuitObserver: NSObject {
    let model: AppModel
    init(model: AppModel) {
        self.model = model
        super.init()
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(willTerminate),
            name: NSApplication.willTerminateNotification,
            object: nil
        )
    }

    @objc private func willTerminate() {
        model.shutdown()
    }
}
