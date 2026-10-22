import Foundation
import Vision
import ImageIO

// Emits one JSON object per recognized line. Coordinates are normalized with
// their origin at the top left, matching PDF page coordinates in documents.py.
struct TextLine: Codable {
    let text: String
    let bbox: [Double]
    let confidence: Float
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 2 else {
    fail("Usage: ocr_macos <image.png>")
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("Unable to decode input image")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesCPUOnly = true
request.usesLanguageCorrection = true
request.recognitionLanguages = ["en-US", "zh-Hans"]
request.minimumTextHeight = 0.001
do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let encoder = JSONEncoder()
    let lines: [TextLine] = (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return TextLine(
            text: candidate.string,
            bbox: [Double(box.minX), Double(1 - box.maxY),
                   Double(box.maxX), Double(1 - box.minY)],
            confidence: candidate.confidence
        )
    }.sorted {
        if abs($0.bbox[1] - $1.bbox[1]) > 0.006 {
            return $0.bbox[1] < $1.bbox[1]
        }
        return $0.bbox[0] < $1.bbox[0]
    }
    for line in lines {
        FileHandle.standardOutput.write(try encoder.encode(line))
        FileHandle.standardOutput.write(Data([10]))
    }
} catch {
    fail("Vision OCR failed: \(error.localizedDescription)")
}
