import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var monitor: BeaconMonitor
    @ObservedObject private var sender = EventSender.shared
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("notifyOnEvent") private var notifyOnEvent = true
    @AppStorage("syncEnabled") private var syncEnabled = false
    @AppStorage("serverURL") private var serverURL = ""
    @State private var tokenDraft = ""
    @State private var tokenSaved = KeychainStore.load() != nil
    @State private var tokenMessage = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("位置情報", value: monitor.permissionText)
                    LabeledContent("監視", value: monitor.monitoringText)
                    LabeledContent("iBeacon", value: monitor.statusText)
                    Button("位置情報と通知を許可") { monitor.askForPermission() }
                    Button("現在の状態を再確認") { monitor.checkNow() }
                } header: {
                    Text("検知状態")
                } footer: {
                    Text("iOSが範囲の出入りを推定します。退室の検知には時間がかかる場合があります。")
                }

                Section {
                    Toggle("入退室をiPhoneに通知", isOn: $notifyOnEvent)
                    Text("UUID：\(BeaconConfig.uuid.uuidString)")
                        .font(.footnote.monospaced())
                        .textSelection(.enabled)
                    Text("Major：\(BeaconConfig.major)  /  Minor：\(BeaconConfig.minor)")
                        .font(.footnote)
                } header: {
                    Text("実験設定")
                }

                Section {
                    Toggle("LabHubサーバーへ送信", isOn: $syncEnabled)
                    TextField("https://...trycloudflare.com", text: $serverURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("デバイストークンを入力", text: $tokenDraft)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("トークンをiPhoneに安全に保存") {
                        tokenSaved = KeychainStore.save(tokenDraft)
                        tokenMessage = tokenSaved ? "保存しました" : "保存できませんでした（32文字以上必要）"
                        if tokenSaved { tokenDraft = "" }
                    }
                    Text("トークン：\(tokenSaved ? "保存済み" : "未登録")  \(tokenMessage)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Button("通信テスト（在室状態は変更しない）") { sender.sendTest() }
                        .disabled(!syncEnabled)
                    Button("現在の状態をサーバーに同期") { monitor.syncCurrentState() }
                        .disabled(!syncEnabled)
                    Button("未送信を再送") { sender.flush() }
                        .disabled(!syncEnabled)
                    LabeledContent("未送信イベント", value: "\(sender.pendingCount)件")
                    Text("通信：\(sender.lastResult)")
                        .font(.caption)
                } header: {
                    Text("サーバー連携（初期設定はオフ）")
                } footer: {
                    Text("LINEの承認済みユーザーに紐づく専用トークンを使用します。URLを変更したらこの画面で更新してください。")
                }

                Section {
                    if monitor.logs.isEmpty {
                        Text("まだログがありません。")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(monitor.logs) { item in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.time)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                                Text(item.message)
                                    .font(.subheadline)
                            }
                        }
                    }
                    Button("ログを消去", role: .destructive) { monitor.clearLogs() }
                } header: {
                    Text("検知ログ（新しい順）")
                }
            }
            .navigationTitle("LabHub Beacon")
        }
        .onAppear {
            monitor.startIfAuthorized()
            sender.flush()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                monitor.checkNow()
                sender.flush()
            }
        }
        .onChange(of: syncEnabled) { _, enabled in
            if enabled { sender.flush() }
        }
    }
}
