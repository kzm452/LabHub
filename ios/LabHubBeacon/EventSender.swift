import Foundation
import Combine
import UIKit

struct LabHubEvent: Codable, Identifiable {
    let id: String
    let event: String
    let trigger: String
    let ts: Int64
    let region: String
}

// 通信に失敗したイベントは端末内に保存し、次の起動/検知時に再送する。
// iOSによる起動とネットワーク送信はベストエフォートであり、即時配信を保証しない。
@MainActor
final class EventSender: ObservableObject {
    static let shared = EventSender()

    @Published private(set) var pendingCount = 0
    @Published private(set) var lastResult = "未送信"

    private let defaults = UserDefaults.standard
    private let queueKey = "labhub.pendingEvents.v4"
    private var sending = false
    private var backgroundTask = UIBackgroundTaskIdentifier.invalid

    private init() { pendingCount = pending.count }

    private var pending: [LabHubEvent] {
        get {
            guard let data = defaults.data(forKey: queueKey) else { return [] }
            return (try? JSONDecoder().decode([LabHubEvent].self, from: data)) ?? []
        }
        set {
            defaults.set(try? JSONEncoder().encode(Array(newValue.suffix(100))), forKey: queueKey)
            pendingCount = min(newValue.count, 100)
        }
    }

    func enqueue(action: String, trigger: String) {
        guard defaults.bool(forKey: "syncEnabled") else { return }
        let event = LabHubEvent(
            id: UUID().uuidString, event: action, trigger: trigger,
            ts: Int64(Date().timeIntervalSince1970 * 1000), region: BeaconConfig.identifier
        )
        pending.append(event)
        flush()
    }

    func sendTest() { enqueue(action: "test", trigger: "test") }

    func flush() {
        guard defaults.bool(forKey: "syncEnabled"), !sending, let first = pending.first else { return }
        guard let request = makeRequest(first) else {
            lastResult = "送信URLまたはデバイストークンを確認してください"
            return
        }
        sending = true
        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "LabHubPresence") { [weak self] in
            Task { @MainActor in self?.finishBackgroundTask() }
        }
        Task { @MainActor in
            var shouldSendNext = false
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                let status = (response as? HTTPURLResponse)?.statusCode ?? -1
                if (200..<300).contains(status) {
                    let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                    let applied = info?["applied"] as? Bool ?? false
                    pending.removeAll { $0.id == first.id }
                    lastResult = "成功（\(first.event)・\(applied ? "在室反映" : "記録のみ")）"
                    shouldSendNext = true
                } else if status == 400 {
                    pending.removeAll { $0.id == first.id }
                    lastResult = "400：不正なイベントを破棄しました"
                    shouldSendNext = true
                } else {
                    lastResult = "HTTP \(status)：未送信のまま保存しました"
                }
            } catch {
                lastResult = "通信できません：未送信のまま保存しました"
            }
            sending = false
            finishBackgroundTask()
            if shouldSendNext { flush() }
        }
    }

    private func finishBackgroundTask() {
        guard backgroundTask != .invalid else { return }
        UIApplication.shared.endBackgroundTask(backgroundTask)
        backgroundTask = .invalid
    }

    private func makeRequest(_ event: LabHubEvent) -> URLRequest? {
        let raw = (defaults.string(forKey: "serverURL") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: raw), parts.scheme?.lowercased() == "https",
              parts.host != nil, parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/",
              let token = KeychainStore.load() else { return nil }
        parts.path = "/api/app/presence"
        guard let url = parts.url else { return nil }
        var request = URLRequest(url: url, timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try? JSONEncoder().encode(event)
        return request
    }
}
