import SwiftUI
import AppKit

struct PreferencesView: View {
    @EnvironmentObject var model: AgentModel
    @State private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            SitesPrefsView().tabItem { Label("Sites", systemImage: "globe") }.tag(0)
            AppsPrefsView().tabItem { Label("Apps", systemImage: "app") }.tag(1)
            SettingsPrefsView().tabItem { Label("Settings", systemImage: "gear") }.tag(2)
            DebugPrefsView().tabItem { Label("Debug", systemImage: "ladybug") }.tag(3)
            HistoryPrefsView().tabItem { Label("History", systemImage: "clock") }.tag(4)
        }
        .padding()
        .environmentObject(model)
        .onAppear { Task { await model.refreshPrefs() } }
    }
}

struct SitesPrefsView: View {
    @EnvironmentObject var model: AgentModel
    @State private var url = ""
    @State private var purpose = ""

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                TextField("https://…", text: $url)
                TextField("purpose", text: $purpose).frame(width: 180)
                Button("Add") {
                    Task {
                        await model.addSite(url: url, purpose: purpose)
                        url = ""; purpose = ""
                    }
                }
                .disabled(url.isEmpty || purpose.isEmpty)
            }
            List {
                ForEach(Array(model.sites.enumerated()), id: \.offset) { _, site in
                    HStack {
                        Text((site["purpose"] as? String) ?? "")
                            .frame(width: 160, alignment: .leading)
                        Text((site["url"] as? String) ?? "")
                            .lineLimit(1)
                        Spacer()
                        if let id = site["id"] as? Int {
                            Button("Delete") { Task { await model.deleteSite(id: id) } }
                        }
                    }
                }
            }
        }
    }
}

struct AppsPrefsView: View {
    @EnvironmentObject var model: AgentModel
    @State private var alias = ""
    @State private var appName = ""

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                TextField("alias", text: $alias).frame(width: 100)
                TextField("App Name", text: $appName)
                Button("Add") {
                    Task {
                        await model.addApp(alias: alias, appName: appName)
                        alias = ""; appName = ""
                    }
                }
                .disabled(alias.isEmpty || appName.isEmpty)
            }
            List {
                ForEach(Array(model.apps.enumerated()), id: \.offset) { _, app in
                    HStack {
                        Text((app["alias"] as? String) ?? "").frame(width: 100, alignment: .leading)
                        Text((app["app_name"] as? String) ?? (app["resolved_target"] as? String) ?? "")
                        Spacer()
                        if let id = app["id"] as? Int {
                            Button("Delete") { Task { await model.deleteApp(id: id) } }
                        }
                    }
                }
            }
        }
    }
}

struct SettingsPrefsView: View {
    @EnvironmentObject var model: AgentModel
    @State private var draft = ""
    @AppStorage(OverlayAutoHide.defaultsKey) private var autoHideSeconds: Int = 15

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("Overlay") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Auto-hide floating panel after")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Picker("Auto-hide", selection: $autoHideSeconds) {
                        ForEach(OverlayAutoHide.choices, id: \.seconds) { choice in
                            Text(choice.label).tag(choice.seconds)
                        }
                    }
                    .pickerStyle(.menu)
                    .labelsHidden()
                    .frame(maxWidth: 220, alignment: .leading)
                    Text(autoHideSeconds == 0
                         ? "Panel stays until you close it (Esc / ✕)."
                         : "Hides \(autoHideSeconds)s after the last activity (typing, answer, or show).")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            GroupBox("Permissions") {
                VStack(alignment: .leading, spacing: 8) {
                    Text(
                        "Accessibility is requested once. Enable MacAgent.app "
                        + "(not AEServer). Screen control uses the running MacAgent app."
                    )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Button("Open Accessibility Settings") {
                        if let url = URL(
                            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
                        ) {
                            NSWorkspace.shared.open(url)
                        }
                    }
                    Text(
                        "Voice in the overlay uses the MacBook mic + macOS Speech Recognition. "
                        + "FreeFlow still works separately if you prefer it."
                    )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                    HStack(spacing: 10) {
                        Button("Microphone Settings") {
                            if let url = URL(
                                string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
                            ) {
                                NSWorkspace.shared.open(url)
                            }
                        }
                        Button("Speech Recognition Settings") {
                            if let url = URL(
                                string: "x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition"
                            ) {
                                NSWorkspace.shared.open(url)
                            }
                        }
                    }
                }
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Text("Notes for the agent (facts about you / preferences). Lines starting with # are ignored.")
                .font(.caption)
                .foregroundStyle(.secondary)
            TextEditor(text: $draft)
                .font(.system(.body, design: .monospaced))
            Button("Save notes") { Task { await model.saveNotes(draft) } }
        }
        .onAppear { draft = model.contextNotes }
        .onChange(of: model.contextNotes) { draft = $0 }
    }
}

struct DebugPrefsView: View {
    @EnvironmentObject var model: AgentModel
    var body: some View {
        ScrollView {
            Text(model.debugJSON)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear { Task { await model.refreshPrefs() } }
    }
}

struct HistoryPrefsView: View {
    @EnvironmentObject var model: AgentModel
    var body: some View {
        List {
            ForEach(Array(model.history.enumerated()), id: \.offset) { _, item in
                VStack(alignment: .leading, spacing: 2) {
                    Text((item["spoken_text"] as? String) ?? (item["utterance"] as? String) ?? "")
                        .font(.headline)
                    Text((item["action"] as? String) ?? "")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if let result = item["result"] as? String, !result.isEmpty {
                        Text(result)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
            }
        }
    }
}
