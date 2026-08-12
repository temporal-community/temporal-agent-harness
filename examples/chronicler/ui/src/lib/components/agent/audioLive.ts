export interface AudioLiveEffectDependencies {
  mountBridge(): () => void;
  subscribeBridge(): () => void;
  refreshStatus(): Promise<void>;
  startPolling(refresh: () => void): unknown;
  stopPolling(timer: unknown): void;
}

export function startAudioLiveEffects(
  following: boolean,
  childMayExist: boolean,
  dependencies: AudioLiveEffectDependencies
): () => void {
  if (!following) return () => undefined;
  const release = dependencies.mountBridge();
  const unsubscribe = dependencies.subscribeBridge();
  if (!childMayExist) {
    return () => {
      unsubscribe();
      release();
    };
  }
  void dependencies.refreshStatus();
  const timer = dependencies.startPolling(() => { void dependencies.refreshStatus(); });
  return () => {
    dependencies.stopPolling(timer);
    unsubscribe();
    release();
  };
}
