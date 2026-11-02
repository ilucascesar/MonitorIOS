import AVFoundation
import SwiftUI
import UIKit

/// Vídeo ao vivo (grade de câmeras): só exibe, sem gestos.
struct LiveVideoView: UIViewRepresentable {
    let view: VideoRenderView
    var gravity: AVLayerVideoGravity = .resizeAspect

    func makeUIView(context: Context) -> VideoHostView {
        let host = VideoHostView(video: view)
        view.displayLayer.videoGravity = gravity
        return host
    }

    func updateUIView(_ host: VideoHostView, context: Context) {
        host.attach(view)
        view.displayLayer.videoGravity = gravity
    }
}

final class VideoHostView: UIView {
    private var video: VideoRenderView

    init(video: VideoRenderView) {
        self.video = video
        super.init(frame: .zero)
        backgroundColor = .black
        clipsToBounds = true
        addSubview(video)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) não usado") }

    func attach(_ v: VideoRenderView) {
        if v !== video || v.superview !== self {
            video = v
            addSubview(v)
            setNeedsLayout()
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        video.frame = bounds
    }
}

/// Vídeo ao vivo com zoom digital: pinça para ampliar, arrastar para mover e toque duplo
/// para ampliar no ponto tocado (ou voltar ao normal).
struct ZoomableLiveVideoView: UIViewRepresentable {
    let view: VideoRenderView

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> ZoomScrollView {
        let scroll = ZoomScrollView(video: view)
        scroll.delegate = context.coordinator
        return scroll
    }

    func updateUIView(_ scroll: ZoomScrollView, context: Context) {
        scroll.attach(view)
    }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        func viewForZooming(in scrollView: UIScrollView) -> UIView? {
            (scrollView as? ZoomScrollView)?.content
        }
    }
}

final class ZoomScrollView: UIScrollView {
    let content = UIView()
    private var video: VideoRenderView
    private var lastSize: CGSize = .zero

    init(video: VideoRenderView) {
        self.video = video
        super.init(frame: .zero)
        backgroundColor = .black
        minimumZoomScale = 1
        maximumZoomScale = 6
        bouncesZoom = true
        showsHorizontalScrollIndicator = false
        showsVerticalScrollIndicator = false
        contentInsetAdjustmentBehavior = .never
        video.displayLayer.videoGravity = .resizeAspect
        addSubview(content)
        content.addSubview(video)
        let doubleTap = UITapGestureRecognizer(target: self, action: #selector(handleDoubleTap(_:)))
        doubleTap.numberOfTapsRequired = 2
        addGestureRecognizer(doubleTap)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) não usado") }

    func attach(_ v: VideoRenderView) {
        if v !== video || v.superview !== content {
            video = v
            v.displayLayer.videoGravity = .resizeAspect
            content.addSubview(v)
            setNeedsLayout()
        }
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        if bounds.size != lastSize {
            // Girou a tela ou mudou o tamanho: volta ao enquadramento normal.
            lastSize = bounds.size
            zoomScale = 1
            content.frame = CGRect(origin: .zero, size: bounds.size)
            contentSize = bounds.size
        }
        video.frame = content.bounds
    }

    @objc private func handleDoubleTap(_ g: UITapGestureRecognizer) {
        if zoomScale > 1.01 {
            setZoomScale(1, animated: true)
            return
        }
        let point = g.location(in: content)
        let scale: CGFloat = 2.5
        let size = CGSize(width: bounds.width / scale, height: bounds.height / scale)
        zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2, width: size.width, height: size.height),
             animated: true)
    }
}
