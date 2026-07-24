import SwiftUI
import AppKit
import ApplicationServices

private enum PrefsPane: String, CaseIterable, Identifiable, Hashable {
    case model, cloud, overlay, voice, privacy, notes
    case sites, apps, history, debug

    var id: String { rawValue }

    var title: String {
        switch self {
        case .model: return "Model"
        case .cloud: return "Cloud"
        case .overlay: return "Overlay"
        case .voice: return "Voice"
        case .privacy: return "Privacy"
        case .notes: return "Notes"
        case .sites: return "Sites"
        case .apps: return "Apps"
        case .history: return "History"
        case .debug: return "Debug"
        }
    }

    var systemImage: String {
        switch self {
        case .model: return "cpu"
        case .cloud: return "cloud"
        case .overlay: return "rectangle.on.rectangle"
        case .voice: return "speaker.wave.2"
        case .privacy: return "lock.shield"
        case .notes: return "note.text"
        case .sites: return "globe"
        case .apps: return "app"
        case .history: return "clock"
        case .debug: return "ladybug"
        }
    }

    static let general: [PrefsPane] = [.model, .cloud, .overlay, .voice, .privacy, .notes]
    static let data: [PrefsPane] = [.sites, .apps, .history, .debug]
}

struct PreferencesView: View {
    @EnvironmentObject var model: AgentModel
    @State private var pane: PrefsPane = .model

    var body: some View {
        NavigationSplitView {
            List(selection: $pane) {
                Section("General") {
                    ForEach(PrefsPane.general) { item in
                        Label(item.title, systemImage: item.systemImage)
                            .tag(item)
                    }
                }
                Section("Data") {
                    ForEach(PrefsPane.data) { item in
                        Label(item.title, systemImage: item.systemImage)
                            .tag(item)
                    }
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 160, ideal: 180, max: 220)
        } detail: {
            Group {
                switch pane {
                case .model: ModelPrefsPane()
                case .cloud: CloudPrefsPane()
                case .overlay: OverlayPrefsPane()
                case .voice: VoicePrefsPane()
                case .privacy: PrivacyPrefsPane()
                case .notes: NotesPrefsPane()
                case .sites: SitesPrefsView()
                case .apps: AppsPrefsView()
                case .history: HistoryPrefsView()
                case .debug: DebugPrefsView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .navigationTitle(pane.title)
        }
        .tint(Theme.accent)
        .environmentObject(model)
        .onAppear { Task { await model.refreshPrefs() } }
    }
}

// MARK: - Shared chrome

private struct PrefsPage<Content: View>: View {
    let subtitle: String
    var section: String? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let section {
                    PrefsSectionLabel(text: section)
                }
                content()
            }
            .padding(20)
            .frame(maxWidth: 640, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background { PrefsBackground() }
    }
}

private struct PrefsCard<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusCard, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusCard, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.06), lineWidth: 1)
        )
    }
}

private struct PrefsRow<Trailing: View>: View {
    let title: String
    var subtitle: String = ""
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.body)
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 12)
            trailing()
        }
    }
}

// MARK: - Model

private struct ModelPrefsPane: View {
    @EnvironmentObject var model: AgentModel
    @State private var selectedModelPath = ""

    var body: some View {
        PrefsPage(
            subtitle: "On-device GGUF for planning and answers. Qwen3-4B is recommended; Llama 3.2, Phi-3.5, Qwen 2.5 7B Q4, and SmolLM also work. Switching reloads the model and uses the matching chat template.",
            section: "Local model"
        ) {
            PrefsCard {
                PrefsRow(
                    title: "Model folder",
                    subtitle: model.modelDir.isEmpty ? "~/Models" : model.modelDir
                ) {
                    Button("Open") {
                        let raw = model.modelDir.isEmpty ? "~/Models" : model.modelDir
                        let expanded = (raw as NSString).expandingTildeInPath
                        NSWorkspace.shared.open(URL(fileURLWithPath: expanded, isDirectory: true))
                    }
                }

                Divider()

                if model.availableModels.isEmpty {
                    Label(
                        "No .gguf files found. Drop an Instruct GGUF into the folder, then refresh.",
                        systemImage: "exclamationmark.triangle.fill"
                    )
                    .font(.callout)
                    .foregroundStyle(Theme.caution)
                    .fixedSize(horizontal: false, vertical: true)
                } else {
                    PrefsRow(title: "Active model", subtitle: statusCaption) {
                        Picker("", selection: $selectedModelPath) {
                            ForEach(model.usableModelPaths, id: \.self) { path in
                                Text(model.menuLabelForModelPath(path)).tag(path)
                            }
                        }
                        .labelsHidden()
                        .pickerStyle(.menu)
                        .frame(maxWidth: 320)
                        .disabled(model.modelSwitching || !model.daemonOnline)
                    }

                    if model.isRecommendedModel(model.modelPath) {
                        Label("Recommended for this Mac", systemImage: "checkmark.seal.fill")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(Theme.accentDeep)
                    }

                    if !model.heavyModelPaths.isEmpty {
                        Text("Skipped (too large for reliable Metal use):")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        ForEach(model.heavyModelPaths, id: \.self) { path in
                            Text(model.labelForModelPath(path))
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                        }
                    }

                    HStack(spacing: 10) {
                        Button(model.modelSwitching ? "Loading…" : "Use selected") {
                            Task { await model.selectModel(path: selectedModelPath) }
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(Theme.accent)
                        .disabled(
                            model.modelSwitching
                                || selectedModelPath.isEmpty
                                || selectedModelPath == model.modelPath
                                || !model.daemonOnline
                                || model.isModelTooHeavy(selectedModelPath)
                        )
                        Button("Refresh list") {
                            Task {
                                await model.refreshModels()
                                syncSelection()
                            }
                        }
                        .disabled(model.modelSwitching)
                        Spacer()
                    }
                }
            }
        }
        .onAppear {
            Task {
                await model.refreshModels()
                syncSelection()
            }
        }
        .onChange(of: model.modelPath) { _ in syncSelection() }
        .onChange(of: model.availableModels.count) { _ in syncSelection() }
    }

    private var statusCaption: String {
        if model.modelSwitching {
            return "Loading into Metal…"
        }
        let name = URL(fileURLWithPath: model.modelPath).lastPathComponent
        if name.isEmpty { return "None selected" }
        return model.modelLoaded ? "\(name) · loaded" : "\(name) · loads on first ask"
    }

    private func syncSelection() {
        if !model.modelPath.isEmpty, !model.isModelTooHeavy(model.modelPath) {
            selectedModelPath = model.modelPath
        } else if let first = model.usableModelPaths.first(where: { model.isRecommendedModel($0) })
            ?? model.usableModelPaths.first
        {
            selectedModelPath = first
        }
    }
}

// MARK: - Cloud

private struct CloudPrefsPane: View {
    @EnvironmentObject var model: AgentModel
    @State private var enabled = false
    @State private var provider: CloudProviderPreset = .openai
    @State private var baseURL = CloudProviderPreset.openai.baseURL
    @State private var apiKeyDraft = ""
    @State private var modelName = CloudProviderPreset.openai.defaultModel
    @State private var routeGeneral = true
    @State private var saving = false

    var body: some View {
        PrefsPage(
            subtitle: "Pick a provider preset, or Custom for any OpenAI-compatible base URL. Mac actions stay on the local GGUF.",
            section: "Inference Engine / Cloud"
        ) {
            PrefsCard {
                Toggle("Enable Cloud Acceleration", isOn: $enabled)
                    .disabled(saving)
                    .onChange(of: enabled) { on in
                        Task {
                            await model.saveCloudSettings(enabled: on, routeGeneral: routeGeneral)
                        }
                    }

                Divider()

                PrefsRow(
                    title: "Provider",
                    subtitle: provider == .custom
                        ? "Enter any OpenAI-compatible base URL below."
                        : "Fills the default base URL and model for this provider."
                ) {
                    Picker("", selection: $provider) {
                        ForEach(CloudProviderPreset.allCases) { preset in
                            Text(preset.label).tag(preset)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                    .frame(width: 180)
                    .disabled(saving)
                    .onChange(of: provider) { preset in
                        applyPreset(preset, replaceModel: true)
                    }
                }

                Divider()

                PrefsRow(
                    title: "Base URL",
                    subtitle: provider == .custom
                        ? "Full API root (app appends /chat/completions)."
                        : provider.baseURL
                ) {
                    TextField("https://…", text: $baseURL)
                        .textFieldStyle(.roundedBorder)
                        .frame(minWidth: 260)
                        .disabled(saving || provider != .custom)
                }

                Divider()

                PrefsRow(
                    title: "API Key",
                    subtitle: model.cloudApiKeySet
                        ? "Stored on this Mac (\(model.cloudApiKeyMasked)). Leave blank to keep."
                        : "Paste your real key, then Save."
                ) {
                    SecureField(provider.keyPlaceholder, text: $apiKeyDraft)
                        .textFieldStyle(.roundedBorder)
                        .frame(minWidth: 220)
                        .disabled(saving)
                }

                Divider()

                PrefsRow(
                    title: "Model name",
                    subtitle: "Default for \(provider.label): \(provider.defaultModel)"
                ) {
                    TextField(provider.defaultModel, text: $modelName)
                        .textFieldStyle(.roundedBorder)
                        .frame(minWidth: 200)
                        .disabled(saving)
                }

                Divider()

                Toggle("Route general knowledge queries to cloud", isOn: $routeGeneral)
                    .disabled(saving || !enabled)

                Divider()

                HStack {
                    Spacer()
                    Button(saving ? "Saving…" : "Save") {
                        Task { await save() }
                    }
                    .disabled(saving || !model.daemonOnline)
                    .keyboardShortcut(.defaultAction)
                }
            }

            Text(
                "Presets: OpenAI, DeepSeek, Google (Gemini), Groq. Custom is for OpenRouter, Together, local gateways, etc.\n\n"
                    + "Privacy: file/app/bash/screen actions always use the local GGUF. Cloud prompts are sanitized "
                    + "before upload. On API failure, MacAgent falls back to local."
            )
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.top, 4)
        }
        .onAppear { syncFromModel() }
        .onChange(of: model.cloudEnabled) { _ in syncFromModel() }
        .onChange(of: model.cloudProvider) { _ in syncFromModel() }
        .onChange(of: model.cloudBaseURL) { _ in syncFromModel() }
        .onChange(of: model.cloudModelName) { _ in syncFromModel() }
        .onChange(of: model.cloudRouteGeneral) { _ in syncFromModel() }
    }

    private func applyPreset(_ preset: CloudProviderPreset, replaceModel: Bool) {
        if preset != .custom {
            baseURL = preset.baseURL
        }
        if replaceModel {
            modelName = preset.defaultModel
        }
    }

    private func syncFromModel() {
        enabled = model.cloudEnabled
        let inferred = CloudProviderPreset.from(id: model.cloudProvider)
        provider = inferred == .custom
            ? CloudProviderPreset.infer(fromBaseURL: model.cloudBaseURL)
            : inferred
        baseURL = model.cloudBaseURL
        modelName = model.cloudModelName
            .replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
            .first?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            ?? model.cloudModelName
        routeGeneral = model.cloudRouteGeneral
    }

    private func save() async {
        saving = true
        defer { saving = false }
        var cleanedURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if provider != .custom {
            cleanedURL = provider.baseURL
        } else {
            let lower = cleanedURL.lowercased()
            if lower.contains("googleapis.com") || lower.contains("gemini") || lower.contains("generativelanguage") {
                if !lower.contains("v1beta/openai") {
                    cleanedURL = CloudProviderPreset.google.baseURL
                }
            }
        }
        let cleanedName = modelName
            .replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
            .first?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            ?? modelName
        await model.saveCloudSettings(
            enabled: enabled,
            provider: provider.rawValue,
            baseURL: cleanedURL,
            apiKey: apiKeyDraft.isEmpty ? nil : apiKeyDraft,
            modelName: cleanedName.isEmpty ? provider.defaultModel : cleanedName,
            routeGeneral: routeGeneral
        )
        apiKeyDraft = ""
        baseURL = cleanedURL
        modelName = cleanedName.isEmpty ? provider.defaultModel : cleanedName
    }
}

// MARK: - Overlay

private struct OverlayPrefsPane: View {
    @AppStorage(OverlayAutoHide.defaultsKey) private var autoHideSeconds: Int = 15

    var body: some View {
        PrefsPage(subtitle: "Floating panel behavior. It always opens in the top-right corner.") {
            PrefsCard {
                PrefsRow(
                    title: "Auto-hide",
                    subtitle: autoHideSeconds == 0
                        ? "Stays open until you press Esc or ✕."
                        : "Hides \(autoHideSeconds)s after the last activity."
                ) {
                    Picker("", selection: $autoHideSeconds) {
                        ForEach(OverlayAutoHide.choices, id: \.seconds) { choice in
                            Text(choice.label).tag(choice.seconds)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                    .frame(width: 160)
                }

                Divider()

                PrefsRow(
                    title: "Panel size",
                    subtitle: "Reset width and height to the default."
                ) {
                    Button("Reset size") {
                        AppDelegate.shared?.resetOverlayPosition()
                    }
                }
            }
        }
    }
}

// MARK: - Voice

private struct VoicePrefsPane: View {
    @EnvironmentObject var model: AgentModel
    @State private var ttsEnabled = true
    @State private var ttsSpeakStatus = true
    @State private var ttsSpeakAnswer = true
    @State private var ttsVolume: Double = 0.95

    var body: some View {
        PrefsPage(subtitle: "Kokoro spoken voice. Quick mute lives on the overlay speaker icon.") {
            PrefsCard {
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

                Divider()

                PrefsRow(title: "Volume", subtitle: String(format: "%.0f%%", ttsVolume * 100)) {
                    Slider(value: $ttsVolume, in: 0.2...1.2, step: 0.05)
                        .frame(width: 180)
                        .disabled(!ttsEnabled)
                        .onChange(of: ttsVolume) { val in
                            Task { await model.saveTTSSettings(volume: val) }
                        }
                }

                Text("Voice: \(model.ttsVoice.isEmpty ? "af_heart" : model.ttsVoice) · first use downloads ~327 MB weights")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
        }
        .onAppear { syncFromModel() }
        .onChange(of: model.ttsEnabled) { ttsEnabled = $0 }
        .onChange(of: model.ttsSpeakStatus) { ttsSpeakStatus = $0 }
        .onChange(of: model.ttsSpeakAnswer) { ttsSpeakAnswer = $0 }
        .onChange(of: model.ttsVolume) { ttsVolume = $0 }
    }

    private func syncFromModel() {
        ttsEnabled = model.ttsEnabled
        ttsSpeakStatus = model.ttsSpeakStatus
        ttsSpeakAnswer = model.ttsSpeakAnswer
        ttsVolume = model.ttsVolume
    }
}

// MARK: - Privacy

private struct PrivacyPrefsPane: View {
    @State private var axTrusted = AXIsProcessTrusted()
    @State private var axMessage = ""

    var body: some View {
        PrefsPage(subtitle: "macOS permissions MacAgent needs for UI control and voice dictation.") {
            PrefsCard {
                PrefsRow(
                    title: "Accessibility",
                    subtitle: axTrusted
                        ? "Granted for this MacAgent.app — click/type should work."
                        : "macOS says this copy is NOT trusted (toggle can look On after a rebuild)."
                ) {
                    HStack(spacing: 8) {
                        Circle()
                            .fill(axTrusted ? Color.green : Color.orange)
                            .frame(width: 8, height: 8)
                        Text(axTrusted ? "On" : "Off")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(axTrusted ? .green : .orange)
                        Button("Request Access") {
                            let result = UIBridgeServer.ensureAccessibility(prompt: true)
                            axTrusted = (result["trusted"] as? Bool) == true
                            axMessage = (result["error"] as? String)
                                ?? (result["message"] as? String)
                                ?? ""
                            openAccessibilitySettings()
                        }
                        Button("Open Settings") {
                            openAccessibilitySettings()
                        }
                    }
                }

                if !axMessage.isEmpty {
                    Text(axMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Text(
                    "After installing a new DMG/build: System Settings → Accessibility → "
                    + "turn MacAgent OFF, then ON, Quit MacAgent, reopen. "
                    + "Enable /Applications/MacAgent.app only — not AEServer or Terminal."
                )
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)

                Divider()

                PrefsRow(
                    title: "Microphone",
                    subtitle: "Used by the overlay mic for on-device dictation."
                ) {
                    Button("Open Settings") {
                        openPref("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
                    }
                }

                Divider()

                PrefsRow(
                    title: "Speech Recognition",
                    subtitle: "Turns mic audio into text on-device."
                ) {
                    Button("Open Settings") {
                        openPref("x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition")
                    }
                }
            }
        }
        .onAppear { refreshAx() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            refreshAx()
        }
    }

    private func refreshAx() {
        axTrusted = AXIsProcessTrusted()
    }

    private func openAccessibilitySettings() {
        openPref("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
    }

    private func openPref(_ raw: String) {
        if let url = URL(string: raw) {
            NSWorkspace.shared.open(url)
        }
    }
}

// MARK: - Notes

private struct NotesPrefsPane: View {
    @EnvironmentObject var model: AgentModel
    @State private var draft = ""
    @State private var savedFlash = false

    var body: some View {
        PrefsPage(subtitle: "Facts about you and preferences the agent should remember. Lines starting with # are ignored.") {
            PrefsCard {
                TextEditor(text: $draft)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 280)
                    .scrollContentBackground(.hidden)

                HStack {
                    if savedFlash {
                        Label("Saved", systemImage: "checkmark.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.green)
                    }
                    Spacer()
                    Button("Save notes") {
                        Task {
                            await model.saveNotes(draft)
                            savedFlash = true
                            try? await Task.sleep(nanoseconds: 1_500_000_000)
                            savedFlash = false
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(draft == model.contextNotes)
                }
            }
        }
        .onAppear { draft = model.contextNotes }
        .onChange(of: model.contextNotes) { draft = $0 }
    }
}

// MARK: - Sites

struct SitesPrefsView: View {
    @EnvironmentObject var model: AgentModel
    @State private var url = ""
    @State private var purpose = ""

    var body: some View {
        PrefsPage(subtitle: "Purpose → site shortcuts the agent can open (e.g. “bank” → your bank URL).") {
            PrefsCard {
                HStack(spacing: 10) {
                    TextField("https://…", text: $url)
                        .textFieldStyle(.roundedBorder)
                    TextField("Purpose", text: $purpose)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 160)
                    Button("Add") {
                        Task {
                            await model.addSite(url: url, purpose: purpose)
                            url = ""
                            purpose = ""
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(url.isEmpty || purpose.isEmpty)
                }
            }

            if model.sites.isEmpty {
                emptyState(
                    icon: "globe",
                    title: "No sites yet",
                    detail: "Add a purpose and URL so MacAgent can open the right page."
                )
            } else {
                PrefsCard {
                    ForEach(Array(model.sites.enumerated()), id: \.offset) { index, site in
                        if index > 0 { Divider() }
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: "link")
                                .foregroundStyle(.secondary)
                                .frame(width: 18)
                            VStack(alignment: .leading, spacing: 2) {
                                Text((site["purpose"] as? String) ?? "")
                                    .font(.headline)
                                Text((site["url"] as? String) ?? "")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                                    .lineLimit(2)
                            }
                            Spacer()
                            if let id = site["id"] as? Int {
                                Button(role: .destructive) {
                                    Task { await model.deleteSite(id: id) }
                                } label: {
                                    Image(systemName: "trash")
                                }
                                .buttonStyle(.borderless)
                                .help("Delete")
                            }
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Apps

struct AppsPrefsView: View {
    @EnvironmentObject var model: AgentModel
    @State private var alias = ""
    @State private var appName = ""

    var body: some View {
        PrefsPage(subtitle: "Spoken aliases for apps (e.g. “chrome” → Google Chrome).") {
            PrefsCard {
                HStack(spacing: 10) {
                    TextField("Alias", text: $alias)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 120)
                    TextField("App Name", text: $appName)
                        .textFieldStyle(.roundedBorder)
                    Button("Add") {
                        Task {
                            await model.addApp(alias: alias, appName: appName)
                            alias = ""
                            appName = ""
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(alias.isEmpty || appName.isEmpty)
                }
            }

            if model.apps.isEmpty {
                emptyState(
                    icon: "app",
                    title: "No app aliases",
                    detail: "Map a short name to a macOS app MacAgent can open."
                )
            } else {
                PrefsCard {
                    ForEach(Array(model.apps.enumerated()), id: \.offset) { index, app in
                        if index > 0 { Divider() }
                        HStack(spacing: 12) {
                            Image(systemName: "app.fill")
                                .foregroundStyle(.secondary)
                                .frame(width: 18)
                            Text((app["alias"] as? String) ?? "")
                                .font(.headline)
                                .frame(width: 100, alignment: .leading)
                            Text((app["app_name"] as? String) ?? (app["resolved_target"] as? String) ?? "")
                                .foregroundStyle(.secondary)
                            Spacer()
                            if let id = app["id"] as? Int {
                                Button(role: .destructive) {
                                    Task { await model.deleteApp(id: id) }
                                } label: {
                                    Image(systemName: "trash")
                                }
                                .buttonStyle(.borderless)
                                .help("Delete")
                            }
                        }
                    }
                }
            }
        }
    }
}

// MARK: - History

struct HistoryPrefsView: View {
    @EnvironmentObject var model: AgentModel

    var body: some View {
        PrefsPage(subtitle: "Recent asks handled by the local daemon.") {
            if model.history.isEmpty {
                emptyState(
                    icon: "clock",
                    title: "No history yet",
                    detail: "Asks from the overlay show up here."
                )
            } else {
                PrefsCard {
                    ForEach(Array(model.history.enumerated()), id: \.offset) { index, item in
                        if index > 0 { Divider() }
                        VStack(alignment: .leading, spacing: 4) {
                            Text(
                                (item["spoken_text"] as? String)
                                    ?? (item["utterance"] as? String)
                                    ?? ""
                            )
                            .font(.system(size: 13, weight: .semibold))
                            .textSelection(.enabled)

                            HStack(spacing: 8) {
                                if let action = item["action"] as? String, !action.isEmpty {
                                    Text(action)
                                        .font(.caption2.weight(.semibold))
                                        .padding(.horizontal, 6)
                                        .padding(.vertical, 2)
                                        .background(Theme.accent.opacity(0.12), in: Capsule())
                                }
                                if let created = item["created_at"] as? String, !created.isEmpty {
                                    Text(created)
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                }
                            }

                            if let result = item["result"] as? String, !result.isEmpty {
                                Text(result)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(3)
                                    .textSelection(.enabled)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
            }
        }
        .onAppear { Task { await model.refreshPrefs() } }
    }
}

private func emptyState(icon: String, title: String, detail: String) -> some View {
    VStack(spacing: 8) {
        Image(systemName: icon)
            .font(.system(size: 28))
            .foregroundStyle(.tertiary)
        Text(title)
            .font(.headline)
        Text(detail)
            .font(.caption)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
    }
    .frame(maxWidth: .infinity)
    .padding(.vertical, 36)
}

// MARK: - Debug

struct DebugPrefsView: View {
    @EnvironmentObject var model: AgentModel
    @State private var selectedId: Int?
    @State private var showRaw = false

    private var selected: DebugTraceItem? {
        guard let selectedId else { return model.debugTraces.first }
        return model.debugTraces.first { $0.id == selectedId } ?? model.debugTraces.first
    }

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("Recent asks")
                        .font(.system(size: 13, weight: .semibold, design: .rounded))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        Task { await model.refreshPrefs() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                    .help("Refresh traces")
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)

                if model.debugTraces.isEmpty {
                    Text("No traces yet — ask something in the overlay.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(16)
                    Spacer()
                } else {
                    List(selection: $selectedId) {
                        ForEach(model.debugTraces) { trace in
                            DebugTraceRowView(trace: trace)
                                .tag(trace.id)
                        }
                    }
                    .listStyle(.sidebar)
                }
            }
            .frame(minWidth: 220, idealWidth: 260, maxWidth: 320)

            Group {
                if let trace = selected {
                    DebugTraceDetailView(trace: trace, showRaw: $showRaw)
                } else {
                    VStack(spacing: 8) {
                        Image(systemName: "ladybug")
                            .font(.system(size: 28))
                            .foregroundStyle(.tertiary)
                        Text("Select a trace")
                            .font(.headline)
                        Text("Inspect route, planner output, tools, and the final answer.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(24)
                }
            }
            .frame(minWidth: 360)
        }
        .background { PrefsBackground() }
        .onAppear {
            Task { await model.refreshPrefs() }
            if selectedId == nil {
                selectedId = model.debugTraces.first?.id
            }
        }
        .onChange(of: model.debugTraces) { traces in
            if selectedId == nil || !traces.contains(where: { $0.id == selectedId }) {
                selectedId = traces.first?.id
            }
        }
    }
}

private struct DebugTraceRowView: View {
    let trace: DebugTraceItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(trace.utterance.isEmpty ? "(empty)" : trace.utterance)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(2)
            HStack(spacing: 6) {
                statusPill
                Text("#\(trace.id)")
                    .foregroundStyle(.tertiary)
                Text("\(trace.steps.count) steps")
                    .foregroundStyle(.secondary)
                if let ts = trace.timestamp {
                    Text(ts, style: .time)
                        .foregroundStyle(.tertiary)
                }
            }
            .font(.caption2)
        }
        .padding(.vertical, 2)
    }

    private var statusPill: some View {
        Text(trace.status.uppercased())
            .font(.system(size: 9, weight: .bold))
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .foregroundStyle(statusColor)
            .background(statusColor.opacity(0.15), in: Capsule())
    }

    private var statusColor: Color {
        switch trace.status.lowercased() {
        case "ok": return Theme.positive
        case "error": return Theme.danger
        case "running": return Theme.caution
        default: return .secondary
        }
    }
}

private struct DebugTraceDetailView: View {
    let trace: DebugTraceItem
    @Binding var showRaw: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(trace.utterance.isEmpty ? "(empty ask)" : trace.utterance)
                        .font(.system(size: 16, weight: .semibold))
                        .textSelection(.enabled)
                    HStack(spacing: 10) {
                        Label(trace.status, systemImage: statusIcon)
                            .foregroundStyle(statusColor)
                        if !trace.source.isEmpty {
                            Text(trace.source)
                                .foregroundStyle(.secondary)
                        }
                        if let finished = trace.finishedAt, let started = trace.timestamp {
                            let ms = Int((finished.timeIntervalSince(started) * 1000).rounded())
                            Text("\(ms) ms")
                                .foregroundStyle(.tertiary)
                        }
                        Spacer()
                    }
                    .font(.caption)
                    if !trace.resultSummary.isEmpty {
                        Text(trace.resultSummary)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: Theme.radiusCard, style: .continuous)
                        .fill(Color.primary.opacity(0.04))
                )

                PrefsSectionLabel(text: "Steps")

                ForEach(trace.steps) { step in
                    DisclosureGroup {
                        Text(step.detail.isEmpty ? "(no detail)" : step.detail)
                            .font(.system(size: 11, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 6)
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Image(systemName: step.isError ? "xmark.circle.fill" : "checkmark.circle.fill")
                                .foregroundStyle(step.isError ? Theme.danger : Theme.accent)
                                .font(.caption)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(step.title)
                                    .font(.system(size: 13, weight: .semibold))
                                Text(step.summary)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(2)
                            }
                        }
                    }
                    .padding(10)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(step.isError ? Theme.danger.opacity(0.08) : Color.primary.opacity(0.03))
                    )
                }

                DisclosureGroup("Raw JSON", isExpanded: $showRaw) {
                    Text(prettyJSON(trace.raw))
                        .font(.system(size: 10, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 6)
                }
                .font(.caption)
            }
            .padding(16)
        }
    }

    private var statusIcon: String {
        switch trace.status.lowercased() {
        case "ok": return "checkmark.seal.fill"
        case "error": return "exclamationmark.triangle.fill"
        case "running": return "hourglass"
        default: return "questionmark.circle"
        }
    }

    private var statusColor: Color {
        switch trace.status.lowercased() {
        case "ok": return Theme.positive
        case "error": return Theme.danger
        case "running": return Theme.caution
        default: return .secondary
        }
    }

    private func prettyJSON(_ obj: [String: Any]) -> String {
        guard JSONSerialization.isValidJSONObject(obj),
              let data = try? JSONSerialization.data(
                withJSONObject: obj,
                options: [.prettyPrinted, .sortedKeys]
              ),
              let text = String(data: data, encoding: .utf8)
        else {
            return String(describing: obj)
        }
        if text.count > 40_000 {
            return String(text.prefix(40_000)) + "…"
        }
        return text
    }
}
