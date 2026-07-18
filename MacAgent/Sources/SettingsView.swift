import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var notes: String = ""
    @State private var preview: String = ""
    @State private var saving = false
    @State private var saveMessage: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Settings")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(.secondary)

                Text("Personal context")
                    .font(.headline)
                Text("MacAgent always injects the current date/time. Add anything else that helps answers — name, city, sports teams, work prefs. Lines starting with # are ignored.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                TextEditor(text: $notes)
                    .font(.system(size: 13, design: .rounded))
                    .frame(minHeight: 160)
                    .padding(8)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.primary.opacity(0.12), lineWidth: 1)
                    )

                HStack {
                    if let saveMessage {
                        Text(saveMessage)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Reload") {
                        Task { await load() }
                    }
                    .disabled(saving)
                    Button("Save") {
                        Task { await save() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(saving)
                }

                Text("Preview (what the model sees)")
                    .font(.headline)
                Text(preview.isEmpty ? "—" : preview)
                    .font(.system(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(Color.primary.opacity(0.05))
                    )
            }
            .padding(20)
            .frame(maxWidth: 640, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .task { await load() }
    }

    private func load() async {
        await model.refreshContext()
        notes = model.userNotes
        preview = model.contextPreview
        saveMessage = nil
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            try await model.saveContext(notes: notes)
            notes = model.userNotes
            preview = model.contextPreview
            saveMessage = "Saved"
        } catch {
            saveMessage = "Save failed"
        }
    }
}
