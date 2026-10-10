import type { RunEvent } from './types';

type Held = { item: RunEvent; since: number };

/**
 * The studio prepares the next play while the current one is heard. Its audio
 * streams ahead, but the screen (score, captions, turns, polls) waits in order
 * until the segment each event follows has finished playing.
 */
export class Presenter {
  private queue: Held[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private pending: (segment: string) => boolean,
    private present: (item: RunEvent, silent: boolean) => void,
    private maxHoldMs = 30000,
  ) {}

  get held() {
    return this.queue.length;
  }

  push(item: RunEvent, historical = false) {
    if (historical && !this.queue.length) {
      this.present(item, false);
      return;
    }
    this.queue.push({ item, since: Date.now() });
    this.drain();
  }

  /** Show everything whose audio has been heard, stopping at the first that is still ahead. */
  drain() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
    while (this.queue.length) {
      const { item, since } = this.queue[0],
        after = item.data.present_after;
      const waiting =
        typeof after === 'string' && this.pending(after) && Date.now() - since < this.maxHoldMs;
      if (waiting) {
        // A stalled device must not freeze the screen forever.
        this.timer = setTimeout(() => this.drain(), 1000);
        return;
      }
      this.queue.shift();
      this.present(item, false);
    }
  }

  /** Apply held state without voices or captions: a new epoch or disconnect superseded it. */
  flush() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
    const held = this.queue;
    this.queue = [];
    for (const { item } of held) this.present(item, true);
  }
}
