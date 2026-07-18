import SwiftUI

struct SitesView: View {
    @EnvironmentObject var model: AppModel
    @State private var urlText = ""
    @State private var purposeText = ""
    @State private var selectedId: Int?
    @State private var busy = false
    @State private var formError: String?

    var body: some View {
        HSplitView {
            List(selection: $selectedId) {
                ForEach(model.sites) { site in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(site.url)
                            .font(.system(size: 13, weight: .semibold, design: .rounded))
                            .lineLimit(1)
                        Text(site.purpose)
                            .font(.system(size: 12, weight: .regular, design: .rounded))
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                    .tag(site.id)
                    .contextMenu {
                        Button("Delete", role: .destructive) {
                            Task { await delete(site.id) }
                        }
                    }
                }
            }
            .frame(minWidth: 240)

            VStack(alignment: .leading, spacing: 12) {
                Text(selectedId == nil ? "Add purpose site" : "Edit purpose site")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(.secondary)

                TextField("https://example.com", text: $urlText)
                    .textFieldStyle(.roundedBorder)
                TextEditor(text: $purposeText)
                    .font(.body)
                    .frame(minHeight: 100)
                    .overlay(
                        RoundedRectangle(cornerRadius: 6)
                            .strokeBorder(Color.secondary.opacity(0.25), lineWidth: 1)
                    )

                if let formError {
                    Text(formError)
                        .foregroundStyle(.red)
                        .font(.caption)
                }

                HStack {
                    Button("New") {
                        selectedId = nil
                        urlText = ""
                        purposeText = ""
                        formError = nil
                    }
                    Spacer()
                    if selectedId != nil {
                        Button("Delete", role: .destructive) {
                            if let id = selectedId {
                                Task { await delete(id) }
                            }
                        }
                        .disabled(busy)
                    }
                    Button(selectedId == nil ? "Add" : "Save") {
                        Task { await save() }
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || urlText.trimmingCharacters(in: .whitespaces).isEmpty
                              || purposeText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }

                Text("Purpose sites: when you say something that matches the purpose, MacAgent opens that URL.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)

                Spacer()
            }
            .padding(20)
            .frame(minWidth: 320)
        }
        .onChange(of: selectedId) { _ in
            guard let newId = selectedId, let site = model.sites.first(where: { $0.id == newId }) else { return }
            urlText = site.url
            purposeText = site.purpose
            formError = nil
        }
    }

    private func save() async {
        busy = true
        formError = nil
        let url = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        let purpose = purposeText.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            if let id = selectedId {
                try await model.updateSite(id: id, url: url, purpose: purpose)
            } else {
                try await model.addSite(url: url, purpose: purpose)
                selectedId = model.sites.last?.id
            }
        } catch {
            formError = "Save failed — is the daemon running?"
        }
        busy = false
    }

    private func delete(_ id: Int) async {
        busy = true
        do {
            try await model.deleteSite(id: id)
            if selectedId == id {
                selectedId = nil
                urlText = ""
                purposeText = ""
            }
        } catch {
            formError = "Delete failed"
        }
        busy = false
    }
}
