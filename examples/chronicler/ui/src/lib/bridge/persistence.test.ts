import { describe, expect, it } from "vitest";
import {
  IndexedDbBridgeRepository
} from "./persistence";
import { audioArtifactReceiptKey } from "./types";

function memoryStorage() {
  const stores = new Map<string, Map<IDBValidKey, unknown>>();
  const store = (name: string) => {
    let values = stores.get(name);
    if (!values) {
      values = new Map();
      stores.set(name, values);
    }
    return values;
  };
  return {
    read: async <T>(storeName: string, key: IDBValidKey) =>
      (store(storeName).get(key) as T | undefined) ?? null,
    write: async (storeName: string, value: unknown, key?: IDBValidKey) => {
      const recordKey = key ?? (value as { key: IDBValidKey }).key;
      store(storeName).set(recordKey, structuredClone(value));
    },
    remove: async (storeName: string, key: IDBValidKey) => {
      store(storeName).delete(key);
    },
    list: async <T>(storeName: string) =>
      [...store(storeName).values()].map((value) => structuredClone(value)) as T[]
  };
}

function receipt(packageRevision = 1) {
  return {
    key: audioArtifactReceiptKey("generation-7", "wav"),
    generationId: "generation-7",
    artifactRole: "wav" as const,
    relativePath: `audio/recap-r${packageRevision}.wav`,
    contentHash: "a".repeat(64),
    contentSize: 44,
    folderBindingId: "binding-a",
    packageRevision,
    operationId: `write-wav-r${packageRevision}`
  };
}

describe("audio artifact receipt persistence", () => {
  it("keys one durable receipt by generation and artifact role", () => {
    expect(audioArtifactReceiptKey("generation/7", "wav")).toBe(
      "generation%2F7|wav"
    );
  });

  it("reloads a durable receipt from a new repository instance", async () => {
    const storage = memoryStorage();
    const stored = receipt();

    await new IndexedDbBridgeRepository(storage).saveAudioArtifactReceipt(stored);

    await expect(
      new IndexedDbBridgeRepository(storage).loadAudioArtifactReceipt(
        "generation-7",
        "wav"
      )
    ).resolves.toEqual(stored);
  });

  it("excludes durable receipts from outbox counts and the rebind guard", async () => {
    const repository = new IndexedDbBridgeRepository(memoryStorage());
    await repository.saveAudioArtifactReceipt(receipt());

    await expect(repository.countOutcomes()).resolves.toBe(0);
    await expect(repository.saveBinding({
      bridgeId: "browser-other",
      rootId: "root-other"
    })).resolves.toBeUndefined();
    await expect(repository.listAudioArtifactReceipts()).resolves.toHaveLength(1);
  });

  it("replaces the role receipt after an approved destination revision", async () => {
    const repository = new IndexedDbBridgeRepository(memoryStorage());
    await repository.saveAudioArtifactReceipt(receipt(1));

    await repository.saveAudioArtifactReceipt(receipt(2));

    await expect(repository.listAudioArtifactReceipts()).resolves.toEqual([
      receipt(2)
    ]);
  });

  it("removes receipts through the ordinary persistence cleanup path", async () => {
    const repository = new IndexedDbBridgeRepository(memoryStorage());
    await repository.saveAudioArtifactReceipt(receipt());

    await repository.removeAudioArtifactReceipt("generation-7", "wav");

    await expect(
      repository.loadAudioArtifactReceipt("generation-7", "wav")
    ).resolves.toBeNull();
  });

  it("rejects a receipt whose key does not match its generation and role", async () => {
    const repository = new IndexedDbBridgeRepository(memoryStorage());

    await expect(repository.saveAudioArtifactReceipt({
      ...receipt(),
      key: "generation-other|wav"
    })).rejects.toThrow("receipt key");
    await expect(repository.listAudioArtifactReceipts()).resolves.toEqual([]);
  });
});
