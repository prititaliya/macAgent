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
    @State private var selectedModelPath = ""
    @State private var ttsEnabled = true
    @State private var ttsSpeakStatus = true
    @State private var ttsSpeakAnswer = true
    @State private var ttsVolume: Double = 0.95
    @AppStorage(OverlayAutoHide.defaultsKey) private var autoHideSeconds: Int = 15

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("Local model") {
                VStack(alignment: .leading, spacing: 8) {
                    Text(
                        model.modelDir.isEmpty
                            ? "Scans ~/Models for .gguf files"
                            : "Folder: \(model.modelDir)"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)

                    if model.availableModels.isEmpty {
                        Text("No .gguf files found. Drop Instruct GGUFs into that folder, then Refresh.")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    } else {
                        Picker("Model", selection: $selectedModelPath) {
                            ForEach(modelPaths, id: \.self) { path in
                                Text(labelForPath(path)).tag(path)
                            }
                        }
                        .pickerStyle(.menu)
                        .disabled(model.modelSwitching || !model.daemonOnline)
                        .frame(maxWidth: .infinity, alignment: .leading)

                        HStack(spacing: 10) {
                            Button(model.modelSwitching ? "Loading…" : "Use selected model") {
                                Task { await model.selectModel(path: selectedModelPath) }
                            }
                            .disabled(
                                model.modelSwitching
                                    || selectedModelPath.isEmpty
                                    || selectedModelPath == model.modelPath
                                    || !model.daemonOnline
                            )
                            Button("Refresh list") {
                                Task {
                                    await model.refreshModels()
                                    syncSelection()
                                }
                            }
                            .disabled(model.modelSwitching)
                        }

                        Text(statusCaption)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .textSelection(.enabled)
                    }
                }
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

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

            GroupBox("Spoken voice (Kokoro)") {
                VStack(alignment: .leading, spacing: 10) {
                    Toggle("Speak while working", isOn: $ttsEnabled)
                        .onChange(of: ttsEnabled) { on in
                            Task { await model.saveTTSSettings(enabled: on) }
                        }
                    Toggle("Status phrases (thinking / researching / acting)", isOn: $ttsSpeakStatus)
                        .disabled(!ttsEnabled)
                        .onChange(of: ttsSpeakStatus) { on in
                            Task { await model.saveTTSSettings(speakStatus: on) }
                        }
                    Toggle("Read final answer aloud", isOn: $ttsSpeakAnswer)
                        .disabled(!ttsEnabled)
                        .onChange(of: ttsSpeakAnswer) { on in
                            Task { await model.saveTTSSettings(speakAnswer: on) }
                        }
                    HStack {
                        Text("Volume")
                        Slider(value: $ttsVolume, in: 0.2...1.2, step: 0.05)
                            .disabled(!ttsEnabled)
                            .onChange(of: ttsVolume) { val in
                                Task { await model.saveTTSSettings(volume: val) }
                            }
                        Text(String(format: "%.0f%%", ttsVolume * 100))
                            .font(.caption.monospacedDigit())
                            .frame(width: 44, alignment: .trailing)
                    }
                    Text("Voice: \(model.ttsVoice.isEmpty ? "af_heart" : model.ttsVoice) · first use downloads ~327 MB weights")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .onAppear {
                    ttsEnabled = model.ttsEnabled
                    ttsSpeakStatus = model.ttsSpeakStatus
                    ttsSpeakAnswer = model.ttsSpeakAnswer
                    ttsVolume = model.ttsVolume
                }
                .onChange(of: model.ttsEnabled) { ttsEnabled = $0 }
                .onChange(of: model.ttsSpeakStatus) { ttsSpeakStatus = $0 }
                .onChange(of: model.ttsSpeakAnswer) { ttsSpeakAnswer = $0 }
                .onChange(of: model.ttsVolume) { ttsVolume = $0 }
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
        .onAppear {
            draft = model.contextNotes
            Task {
                await model.refreshModels()
                syncSelection()
            }
        }
        .onChange(of: model.contextNotes) { draft = $0 }
        .onChange(of: model.modelPath) { _ in syncSelection() }
        .onChange(of: model.availableModels.count) { _ in syncSelection() }
    }

    private var modelPaths: [String] {
        model.availableModels.compactMap { $0["path"] as? String }
    }

    private var statusCaption: String {
        if model.modelSwitching {
            return "Loading GGUF into Metal — this can take a few seconds…"
        }
        let name = URL(fileURLWithPath: model.modelPath).lastPathComponent
        if name.isEmpty {
            return "No model selected."
        }
        let state = model.modelLoaded ? "loaded" : "selected (loads on first ask)"
        return "Active: \(name) — \(state)"
    }

    private func labelForPath(_ path: String) -> String {
        let item = model.availableModels.first { ($0["path"] as? String) == path }
        let name = item?["name"] as? String
            ?? URL(fileURLWithPath: path).lastPathComponent
        if let gb = item?["size_gb"] as? Double {
            return String(format: "%@ (%.1f GB)", name, gb)
        }
        if let gb = item?["size_gb"] as? Int {
            return "\(name) (\(gb) GB)"
        }
        return name
    }

    private func syncSelection() {
        if !model.modelPath.isEmpty {
            selectedModelPath = model.modelPath
        } else if let first = modelPaths.first {
            selectedModelPath = first
        }
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
