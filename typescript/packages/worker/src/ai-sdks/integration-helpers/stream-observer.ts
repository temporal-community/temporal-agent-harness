// The live-streaming observer contract every model integration's streamed activity uses, as
// Python's integration_helpers/_stream_observer.py. The activity hands each of one call's raw SDK
// events to a fresh observer as it arrives; the observer translates them into harness events and
// publishes them to the turn from the activity (`publisherFromActivity`). The routing token, the
// turn's stream context and the model the workflow asked for, rides the activity input.

import type { Duration } from "@temporalio/common";

/** Consumes the raw events of one streamed model call, live, in the activity. */
export interface StreamObserver<RawEvent> {
  /** Before the first event: opens the turn's publisher and the model-interaction bracket. */
  start(): Promise<void>;
  /** Each raw event, in arrival order. */
  onEvent(event: RawEvent): void;
  /** After the last event or the stream's failure (`error`): closes the bracket and drains. */
  finish(error?: unknown): Promise<void>;
}

/** A fresh observer per call, so concurrent calls on one worker never share per-call state. */
export type ObserverFactory<Token, RawEvent> = (
  token: Token,
  options: { batchInterval?: Duration },
) => StreamObserver<RawEvent>;

/** Runs `stream` through `observer`: started first, finished last, whatever the stream does. */
export async function observeStream<RawEvent>(
  observer: StreamObserver<RawEvent>,
  stream: AsyncIterable<RawEvent>,
  onEvent?: (event: RawEvent) => void,
): Promise<void> {
  await observer.start();
  let failure: unknown;
  try {
    for await (const event of stream) {
      onEvent?.(event);
      observer.onEvent(event);
    }
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    await observer.finish(failure);
  }
}
