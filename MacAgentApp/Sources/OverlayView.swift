import SwiftUI
import AppKit

struct OverlayView: View {
    @ObservedObject var model: AgentModel
    var onDismiss: () -> Void
    var onPrefs: () -> Void
    var onQuit: () -> Void = { AppDelegate.shared?.quitApp() }
    var onInteract: () -> Void = {}
    @AppStorage(OverlayAutoHide.defaultsKey) private var autoHideSeconds: Int = 15
    @AppStorage("macagent.searchMode") private var searchMode: String = "auto"
    @State private var draft = ""
    @State private var logsExpanded = false
    @FocusState private var focused: Bool
    @StateObject private var speech = SpeechCapture()

    private var logSteps: [TraceStep] {
        model.traceSteps.filter { $0.title != "Answer" && $0.title != "Input" }
    }

    private var hasAnswer: Bool {
        !model.answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var countdownLabel: String {
        guard let left = model.hideCountdown, autoHideSeconds > 0 else {
            return autoHideSeconds == 0 ? "Auto-hide off" : ""
        }
        if model.busy || model.pendingConfirm != nil {
            return "Paused · \(left)s"
        }
        return "Hides in \(left)s"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            inputBar
            optionChips

            if model.busy {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(model.statusLine.isEmpty ? "Working…" : model.statusLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else if speech.isListening {
                HStack(spacing: 8) {
                    TimelineView(.animation(minimumInterval: 0.45, paused: false)) { context in
                        Circle()
                            .fill(Theme.danger)
                            .frame(width: 7, height: 7)
                            .opacity(
                                Int(context.date.timeIntervalSinceReferenceDate * 2) % 2 == 0
                                    ? 1.0 : 0.25
                            )
                    }
                    Text(speech.statusMessage.isEmpty ? "Listening…" : speech.statusMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else if !speech.statusMessage.isEmpty {
                Text(speech.statusMessage)
                    .font(.caption)
                    .foregroundStyle(Theme.caution)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if !model.lastQuestion.isEmpty {
                        Text(model.lastQuestion)
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }

                    if let pending = model.pendingConfirm {
                        permissionCard(pending)
                    }

                    if hasAnswer {
                        answerCard
                    }

                    if !model.sources.isEmpty {
                        sourcesSection
                    }

                    if !logSteps.isEmpty || model.busy {
                        activityLogs
                    }
                }
            }
            .simultaneousGesture(
                DragGesture(minimumDistance: 1).onChanged { _ in onInteract() }
            )

            footer
        }
        .padding(18)
        .frame(minWidth: 420, minHeight: 280)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background {
            OverlaySurface(urgent: model.hideUrgency, urgentPulse: model.hidePulse)
        }
        .scaleEffect(model.hideUrgency && model.hidePulse ? 1.015 : 1.0)
        .animation(.easeInOut(duration: 0.2), value: model.hidePulse)
        .tint(Theme.accent)
        .onAppear {
            Task {
                await model.refreshHealth()
                await model.refreshModels()
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                focused = true
            }
            onInteract()
            logsExpanded = !hasAnswer
        }
        .onChange(of: model.lastQuestion) { _ in
            if model.busy || !hasAnswer {
                logsExpanded = true
            }
        }
        .onChange(of: autoHideSeconds) { _ in
            onInteract()
        }
        .onChange(of: model.isDictating) { dictating in
            if !dictating && speech.isListening {
                speech.cancel()
            }
        }
    }

    // MARK: - Cards

    private func permissionCard(_ pending: PendingConfirm) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Permission needed", systemImage: "hand.raised.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.cautionDeep)
            Text(pending.summary)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.primary)
                .textSelection(.enabled)
            if !pending.command.isEmpty {
                Text(pending.command)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            HStack(spacing: 10) {
                Button("Deny") {
                    onInteract()
                    Task { await model.respondToConfirm(approve: false) }
                }
                .buttonStyle(GhostButtonStyle())
                Button("Approve") {
                    onInteract()
                    Task { await model.respondToConfirm(approve: true) }
                }
                .buttonStyle(AccentButtonStyle(tint: Theme.cautionDeep))
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusCard, style: .continuous)
                .fill(Theme.caution.opacity(0.10))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusCard, style: .continuous)
                .strokeBorder(Theme.caution.opacity(0.45), lineWidth: 1.25)
        )
    }

    private var answerCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Label("Answer", systemImage: "checkmark.seal.fill")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.accentDeep)
                Text("by \(model.modelDisplayName)")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            Text(model.answer)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(.primary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusCard, style: .continuous)
                .fill(Theme.accent.opacity(0.10))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusCard, style: .continuous)
                .strokeBorder(Theme.accent.opacity(0.35), lineWidth: 1.25)
        )
    }

    private var sourcesSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Sources")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            ForEach(model.sources) { src in
                Button {
                    onInteract()
                    model.openURL(src.url)
                } label: {
                    Text(src.title.isEmpty ? src.url : src.title)
                        .font(.caption)
                        .foregroundStyle(Theme.accentDeep)
                        .lineLimit(2)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var activityLogs: some View {
        DisclosureGroup(isExpanded: $logsExpanded) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(logSteps) { step in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(step.title)
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.tertiary)
                        Text(step.body)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(8)
                    .background(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(Color.primary.opacity(0.04))
                    )
                }
            }
            .padding(.top, 6)
        } label: {
            HStack {
                Image(systemName: "chevron.right.circle")
                    .rotationEffect(.degrees(logsExpanded ? 90 : 0))
                Text(logsExpanded ? "Hide activity logs" : "Show activity logs")
                    .font(.caption.weight(.medium))
                Text("(\(logSteps.count))")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Spacer()
            }
            .foregroundStyle(.secondary)
            .contentShape(Rectangle())
        }
        .onChange(of: hasAnswer) { answered in
            if answered {
                withAnimation(.easeInOut(duration: 0.2)) {
                    logsExpanded = false
                }
            }
        }
        .onChange(of: model.busy) { busy in
            if busy {
                logsExpanded = true
            }
        }
    }

    // MARK: - Header / input

    private var header: some View {
        HStack(spacing: 10) {
            BrandMark(size: 30)
            VStack(alignment: .leading, spacing: 1) {
                Text("MacAgent")
                    .font(.brand(14, .semibold))
                    .foregroundStyle(.primary.opacity(0.9))
                Text("⌃⌥Space")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
            }
            Spacer()
            Button {
                onInteract()
                Task { await model.toggleMute() }
            } label: {
                Image(systemName: model.ttsMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
            }
            .buttonStyle(IconControlButtonStyle(
                active: model.ttsMuted,
                activeColor: Theme.caution
            ))
            .help(model.ttsMuted ? "Unmute voice (tap to hear answers again)" : "Mute voice")

            Button(action: onPrefs) {
                Image(systemName: "gearshape")
            }
            .buttonStyle(IconControlButtonStyle())
            .help("Preferences")

            Button(action: onDismiss) {
                Image(systemName: "xmark")
            }
            .buttonStyle(IconControlButtonStyle())
            .help("Hide overlay")
            .keyboardShortcut(.escape, modifiers: [])

            Button(action: onQuit) {
                Image(systemName: "power")
            }
            .buttonStyle(IconControlButtonStyle())
            .help("Quit MacAgent")
        }
    }

    private var inputBar: some View {
        HStack(spacing: 10) {
            Button {
                onInteract()
                Task { await toggleMic() }
            } label: {
                Image(systemName: speech.isListening ? "mic.fill" : "mic")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(speech.isListening ? Theme.danger : Color.secondary)
                    .frame(width: 32, height: 32)
                    .background(
                        Circle()
                            .fill(
                                speech.isListening
                                    ? Theme.danger.opacity(0.16)
                                    : Color.primary.opacity(0.06)
                            )
                    )
            }
            .buttonStyle(.plain)
            .help(speech.isListening ? "Stop listening" : "Dictate with Mac speech recognition")
            .disabled(model.busy && !speech.isListening)
            .accessibilityLabel(speech.isListening ? "Stop dictation" : "Start dictation")

            TextField(
                speech.isListening ? "Listening… tap mic to send" : "Ask or tell MacAgent…",
                text: $draft
            )
            .textFieldStyle(.plain)
            .font(.system(size: 17, weight: .medium))
            .focused($focused)
            .onSubmit { send() }
            .onChange(of: draft) { _ in onInteract() }
            .onChange(of: speech.partialText) { partial in
                if speech.isListening, !partial.isEmpty {
                    draft = partial
                    onInteract()
                }
            }

            Button(speech.isListening ? "Stop" : "Send") {
                if speech.isListening {
                    Task { await toggleMic() }
                } else {
                    send()
                }
            }
            .buttonStyle(
                AccentButtonStyle(tint: speech.isListening ? Theme.danger : Theme.accent)
            )
            .disabled(
                speech.isListening
                    ? false
                    : (draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
            )
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: Theme.radiusControl, style: .continuous)
                .fill(Color.primary.opacity(0.06))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.radiusControl, style: .continuous)
                .strokeBorder(
                    speech.isListening ? Theme.danger.opacity(0.45) : Color.white.opacity(0.08),
                    lineWidth: speech.isListening ? 1.5 : 1
                )
        )
        .contentShape(Rectangle())
        .onTapGesture {
            focused = true
            onInteract()
        }
    }

    private var optionChips: some View {
        HStack(spacing: 8) {
            Menu {
                if model.modelPaths.isEmpty {
                    Text("No models in ~/Models")
                } else {
                    ForEach(model.usableModelPaths, id: \.self) { path in
                        Button {
                            onInteract()
                            Task { await model.selectModel(path: path) }
                        } label: {
                            HStack {
                                Text(model.menuLabelForModelPath(path))
                                if path == model.modelPath {
                                    Image(systemName: "checkmark")
                                }
                            }
                        }
                        .disabled(model.modelSwitching || path == model.modelPath)
                    }
                    let heavy = model.heavyModelPaths
                    if !heavy.isEmpty {
                        Divider()
                        Text("Too large for this Mac")
                        ForEach(heavy, id: \.self) { path in
                            Text(model.labelForModelPath(path))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            } label: {
                OverlayChip(
                    systemImage: "cpu",
                    title: "Model",
                    value: model.modelSwitching ? "Loading…" : model.modelDisplayName,
                    active: model.modelSwitching
                )
            }
            .disabled(model.modelSwitching || !model.daemonOnline)
            .help("Switch local GGUF model")

            Menu {
                Button {
                    onInteract()
                    searchMode = "auto"
                } label: {
                    searchMenuRow("Auto", selected: searchMode == "auto")
                }
                Button {
                    onInteract()
                    searchMode = "on"
                } label: {
                    searchMenuRow("On", selected: searchMode == "on")
                }
                Button {
                    onInteract()
                    searchMode = "off"
                } label: {
                    searchMenuRow("Off", selected: searchMode == "off")
                }
            } label: {
                OverlayChip(
                    systemImage: "magnifyingglass",
                    title: "Search",
                    value: searchModeLabel,
                    active: searchMode != "auto"
                )
            }
            .help("Web search: Auto (when needed), On, or Off")

            Spacer(minLength: 0)
        }
    }

    private var searchModeLabel: String {
        switch searchMode.lowercased() {
        case "on": return "On"
        case "off": return "Off"
        default: return "Auto"
        }
    }

    private func searchMenuRow(_ title: String, selected: Bool) -> some View {
        HStack {
            Text(title)
            if selected {
                Image(systemName: "checkmark")
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(
                    model.daemonOnline
                        ? Theme.positive
                        : (model.lastError != nil ? Theme.danger : Theme.caution)
                )
                .frame(width: 7, height: 7)
            Text(
                model.daemonOnline
                    ? "Daemon ready"
                    : (model.lastError != nil ? "Daemon failed — see Logs/MacAgent" : "Starting daemon…")
            )
            .font(.caption2)
            .foregroundStyle(.tertiary)
            Spacer()
            if autoHideSeconds > 0, let left = model.hideCountdown {
                HStack(spacing: 6) {
                    if model.hideUrgency {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.caption2)
                            .foregroundStyle(Theme.caution)
                            .opacity(model.hidePulse ? 1 : 0.35)
                    }
                    Text(countdownLabel)
                        .font(.metric(11, model.hideUrgency ? .bold : .medium))
                        .monospacedDigit()
                        .foregroundStyle(model.hideUrgency ? Theme.caution : Color.secondary)
                    Capsule()
                        .fill(Color.primary.opacity(0.10))
                        .frame(width: 44, height: 4)
                        .overlay(alignment: .leading) {
                            Capsule()
                                .fill(model.hideUrgency ? Theme.caution : Theme.accent.opacity(0.85))
                                .frame(
                                    width: max(4, 44 * CGFloat(left) / CGFloat(max(autoHideSeconds, 1))),
                                    height: 4
                                )
                        }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(
                    Capsule()
                        .fill(
                            model.hideUrgency
                                ? Theme.caution.opacity(0.16)
                                : Color.primary.opacity(0.05)
                        )
                )
            } else if autoHideSeconds == 0 {
                Text("Auto-hide off")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private func send() {
        let q = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        draft = ""
        logsExpanded = true
        onInteract()
        Task { await model.ask(q, useWeb: searchMode) }
    }

    private func toggleMic() async {
        onInteract()
        if speech.isListening {
            await model.setDictating(false)
            let text = speech.stop()
            if !text.isEmpty {
                draft = text
                send()
            }
        } else {
            await model.setDictating(true)
            await speech.start()
            if !speech.isListening {
                await model.setDictating(false)
            }
            onInteract()
        }
    }
}
