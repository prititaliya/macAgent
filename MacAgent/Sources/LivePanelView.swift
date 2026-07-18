import SwiftUI

struct LivePanelView: View {
    @EnvironmentObject var model: AppModel
    @FocusState private var queryFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Live")
                .font(.system(size: 13, weight: .semibold, design: .rounded))
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 12) {
                if let event = model.event {
                    Text("You")
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundStyle(.secondary)
                    Text(event.utterance.isEmpty ? "…" : event.utterance)
                        .font(.system(size: 17, weight: .semibold, design: .rounded))
                        .fixedSize(horizontal: false, vertical: true)

                    Divider().opacity(0.35)

                    Text(kindLabel(event))
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundStyle(kindColor(event))
                    Text(event.text)
                        .font(.system(size: 15, weight: .regular, design: .rounded))
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                } else {
                    Text(model.statusLine)
                        .font(.system(size: 15, weight: .medium, design: .rounded))
                        .foregroundStyle(.secondary)
                    Text("Type a question or command below — or use FreeFlow Fn. Edit anytime, then Send.")
                        .font(.system(size: 13, weight: .regular, design: .rounded))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(.ultraThinMaterial)
                    .overlay(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .strokeBorder(Color.primary.opacity(0.08), lineWidth: 1)
                    )
            )

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("Query")
                        .font(.system(size: 12, weight: .semibold, design: .rounded))
                        .foregroundStyle(.secondary)
                    Spacer()
                    if model.event?.utterance.isEmpty == false {
                        Button("Reuse last") {
                            model.loadLastIntoDraft()
                            queryFocused = true
                        }
                        .font(.caption)
                        .buttonStyle(.borderless)
                    }
                }

                TextEditor(text: $model.draftQuery)
                    .font(.system(size: 14, design: .rounded))
                    .frame(minHeight: 72, maxHeight: 120)
                    .padding(8)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(Color(nsColor: .textBackgroundColor))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.primary.opacity(0.12), lineWidth: 1)
                    )
                    .focused($queryFocused)

                HStack {
                    if let err = model.lastError {
                        Text(err)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                    Spacer()
                    Button {
                        Task { await model.sendDraft() }
                    } label: {
                        if model.sendingQuery || model.isPending {
                            ProgressView()
                                .controlSize(.small)
                                .padding(.horizontal, 8)
                        } else {
                            Text("Send")
                        }
                    }
                    .keyboardShortcut(.return, modifiers: [.command])
                    .disabled(
                        model.draftQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                            || model.sendingQuery
                    )
                    .buttonStyle(.borderedProminent)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color.primary.opacity(0.04))
            )

            Text("⌘↩ to send. Voice (FreeFlow) and typing use the same local answer-or-act path.")
                .font(.system(size: 12, weight: .regular, design: .rounded))
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: 560, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .center)
        .onAppear { queryFocused = true }
    }

    private func kindLabel(_ event: AgentEvent) -> String {
        if event.isPending { return "Working" }
        switch event.kind {
        case "answer": return "Answer"
        case "action": return "Action"
        case "error": return "Error"
        default: return event.kind.capitalized
        }
    }

    private func kindColor(_ event: AgentEvent) -> Color {
        if event.isPending { return .orange }
        switch event.kind {
        case "answer": return .blue
        case "action": return .green
        case "error": return .red
        default: return .secondary
        }
    }
}
