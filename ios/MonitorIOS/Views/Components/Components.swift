import AVFoundation
import SwiftUI
import UIKit

// MARK: - Player

final class PlayerUIView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
}

struct PlayerLayerView: UIViewRepresentable {
    let player: AVPlayer
    var gravity: AVLayerVideoGravity = .resizeAspect

    func makeUIView(context: Context) -> PlayerUIView {
        let v = PlayerUIView()
        v.backgroundColor = .black
        v.playerLayer.player = player
        v.playerLayer.videoGravity = gravity
        return v
    }

    func updateUIView(_ v: PlayerUIView, context: Context) {
        if v.playerLayer.player !== player { v.playerLayer.player = player }
        v.playerLayer.videoGravity = gravity
    }
}

// MARK: - Indicadores

struct StatusDot: View {
    let online: Bool
    var body: some View {
        Circle()
            .fill(online ? Color.green : Color.red)
            .frame(width: 8, height: 8)
            .overlay(Circle().stroke(.black.opacity(0.3), lineWidth: 1))
            .accessibilityLabel(online ? "online" : "offline")
    }
}

struct RecBadge: View {
    let recording: Bool
    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(recording ? Color.red : Color.gray).frame(width: 6, height: 6)
            Text(recording ? "REC" : "PARADO").font(.caption2.weight(.bold))
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 3)
        .background(.black.opacity(0.55), in: Capsule())
        .foregroundStyle(.white)
    }
}

struct Pill: View {
    let text: String
    var systemImage: String?
    var color: Color = .secondary
    var body: some View {
        HStack(spacing: 4) {
            if let systemImage { Image(systemName: systemImage) }
            Text(text).lineLimit(1)
        }
        .font(.caption.weight(.medium))
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(color.opacity(0.15), in: Capsule())
        .foregroundStyle(color)
    }
}

struct NoticeBanner: View {
    let text: String
    var actionTitle: String?
    var action: (() -> Void)?
    var tint: Color = .orange

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(tint)
            Text(text).font(.footnote).frame(maxWidth: .infinity, alignment: .leading)
            if let actionTitle, let action {
                Button(actionTitle, action: action).font(.footnote.weight(.semibold)).buttonStyle(.borderedProminent).controlSize(.small)
            }
        }
        .padding(10)
        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - Imagem autenticada (miniaturas)

struct AuthImage: View {
    @EnvironmentObject var model: AppModel
    let path: String
    var refresh: TimeInterval = 30
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        ZStack {
            Color.black
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else if failed {
                Image(systemName: "video.slash").font(.title2).foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .clipped()
        .task(id: path) {
            while !Task.isCancelled {
                do {
                    let data = try await model.api.data(path)
                    if let img = UIImage(data: data) {
                        image = img
                        failed = false
                    }
                } catch {
                    if image == nil { failed = true }
                }
                try? await Task.sleep(nanoseconds: UInt64(refresh * 1_000_000_000))
            }
        }
    }
}

// MARK: - Compartilhamento

struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}

struct SharePayload: Identifiable {
    let id = UUID()
    let items: [Any]
}

// MARK: - Cartão

struct Card<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
    }
}
