import Foundation
import Network

/// Observa mudanças de rede (Wi‑Fi <-> dados móveis, perda de sinal) para reconectar.
@MainActor
final class NetworkMonitor: ObservableObject {
    @Published private(set) var isOnline = true
    @Published private(set) var interfaceName = ""
    var onChange: (() -> Void)?

    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "monitorios.network")
    private var lastSignature = ""

    func start() {
        monitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            let name: String
            if path.usesInterfaceType(.wifi) { name = "Wi‑Fi" }
            else if path.usesInterfaceType(.cellular) { name = "Dados móveis" }
            else if path.usesInterfaceType(.wiredEthernet) { name = "Cabo" }
            else { name = online ? "Rede" : "Sem rede" }
            Task { @MainActor [weak self] in
                guard let self else { return }
                let signature = "\(online)-\(name)"
                let changed = signature != self.lastSignature
                self.lastSignature = signature
                self.isOnline = online
                self.interfaceName = name
                if changed && online { self.onChange?() }
            }
        }
        monitor.start(queue: queue)
    }
}
