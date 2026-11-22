// Runs on the audio rendering thread: groups microphone samples into 20 ms frames
// and posts them (with their RMS level) to the main thread for voice-activity detection.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const ms = (options.processorOptions && options.processorOptions.frameMs) || 20;
    this.size = Math.max(128, Math.round(sampleRate * ms / 1000));
    this.buf = new Float32Array(this.size);
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      let i = 0;
      while (i < ch.length) {
        const take = Math.min(ch.length - i, this.size - this.n);
        this.buf.set(ch.subarray(i, i + take), this.n);
        this.n += take; i += take;
        if (this.n === this.size) {
          let s = 0;
          for (let k = 0; k < this.size; k++) s += this.buf[k] * this.buf[k];
          const rms = Math.sqrt(s / this.size);
          const frame = this.buf.slice(0);
          this.port.postMessage({ frame, rms }, [frame.buffer]);
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("capture", CaptureProcessor);
