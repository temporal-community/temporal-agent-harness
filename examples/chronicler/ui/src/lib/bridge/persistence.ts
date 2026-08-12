import {
  audioArtifactReceiptKey,
  type AudioArtifactReceipt,
  type AudioArtifactReceiptRepository,
  type AudioArtifactRole,
  type DirectoryHandleRepository,
  type StoredLocalOperationOutcome
} from "./types";

const databaseName = "temporal-agent-harness.browser-bridge.v3";
const configStore = "config";
const outcomeStore = "outcomes";
const audioArtifactReceiptStore = "audio-artifact-receipts";

export interface BridgePersistenceStorage {
  read<T>(storeName: string, key: IDBValidKey): Promise<T | null>;
  write(storeName: string, value: unknown, key?: IDBValidKey): Promise<void>;
  remove(storeName: string, key: IDBValidKey): Promise<void>;
  list<T>(storeName: string): Promise<T[]>;
}

export const demoBridgeBinding = {
  bridgeId: "browser-local",
  rootId: "campaign-root"
} as const;

interface StoredDirectory {
  handle: FileSystemDirectoryHandle;
  rootId: string;
  handleBindingId: string;
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.addEventListener("success", () => resolve(request.result), { once: true });
    request.addEventListener("error", () => reject(request.error), { once: true });
  });
}

function transactionComplete(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.addEventListener("complete", () => resolve(), { once: true });
    transaction.addEventListener("abort", () => reject(transaction.error), { once: true });
    transaction.addEventListener("error", () => reject(transaction.error), { once: true });
  });
}

function openDatabase(): Promise<IDBDatabase> {
  const request = indexedDB.open(databaseName, 2);
  request.addEventListener("upgradeneeded", () => {
    const database = request.result;
    if (!database.objectStoreNames.contains(configStore)) {
      database.createObjectStore(configStore);
    }
    if (!database.objectStoreNames.contains(outcomeStore)) {
      database.createObjectStore(outcomeStore, { keyPath: "key" });
    }
    if (!database.objectStoreNames.contains(audioArtifactReceiptStore)) {
      database.createObjectStore(audioArtifactReceiptStore, { keyPath: "key" });
    }
  });
  return requestResult(request);
}

async function readValue<T>(storeName: string, key: IDBValidKey): Promise<T | null> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(storeName, "readonly");
    const value = await requestResult(transaction.objectStore(storeName).get(key));
    await transactionComplete(transaction);
    return (value as T | undefined) ?? null;
  } finally {
    database.close();
  }
}

async function writeValue(
  storeName: string,
  value: unknown,
  key?: IDBValidKey
): Promise<void> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(storeName, "readwrite");
    if (key === undefined) transaction.objectStore(storeName).put(value);
    else transaction.objectStore(storeName).put(value, key);
    await transactionComplete(transaction);
  } finally {
    database.close();
  }
}

async function removeValue(storeName: string, key: IDBValidKey): Promise<void> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(storeName, "readwrite");
    transaction.objectStore(storeName).delete(key);
    await transactionComplete(transaction);
  } finally {
    database.close();
  }
}

async function listValues<T>(storeName: string): Promise<T[]> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(storeName, "readonly");
    const values = await requestResult(transaction.objectStore(storeName).getAll());
    await transactionComplete(transaction);
    return values as T[];
  } finally {
    database.close();
  }
}

const indexedDbStorage: BridgePersistenceStorage = {
  read: readValue,
  write: writeValue,
  remove: removeValue,
  list: listValues
};

export class IndexedDbBridgeRepository implements
  DirectoryHandleRepository,
  AudioArtifactReceiptRepository {
  constructor(
    private readonly storage: BridgePersistenceStorage = indexedDbStorage
  ) {}

  async getBridgeId(): Promise<string> {
    const stored = await this.storage.read<string>(configStore, "bridge-id");
    if (stored) return stored;
    const bridgeId = demoBridgeBinding.bridgeId;
    await this.storage.write(configStore, bridgeId, "bridge-id");
    await this.storage.write(configStore, demoBridgeBinding.rootId, "root-id");
    return bridgeId;
  }

  async saveBinding(binding: { bridgeId: string; rootId: string }): Promise<void> {
    if (await this.countOutcomes()) {
      throw new Error("Cannot change bridge routing while operation results are pending.");
    }
    await this.storage.write(configStore, binding.bridgeId, "bridge-id");
    await this.storage.write(configStore, binding.rootId, "root-id");
    const directory = await this.loadDirectory();
    if (directory) {
      await this.storage.write(
        configStore,
        { ...directory, rootId: binding.rootId } satisfies StoredDirectory,
        "directory"
      );
    }
  }

  async loadDirectory(): Promise<StoredDirectory | null> {
    return this.storage.read<StoredDirectory>(configStore, "directory");
  }

  async saveDirectory(handle: FileSystemDirectoryHandle): Promise<{
    rootId: string;
    handleBindingId: string;
  }> {
    const rootId = await this.storage.read<string>(configStore, "root-id")
      ?? demoBridgeBinding.rootId;
    const handleBindingId = crypto.randomUUID();
    await this.storage.write(
      configStore,
      { handle, rootId, handleBindingId } satisfies StoredDirectory,
      "directory"
    );
    return { rootId, handleBindingId };
  }

  async countOutcomes(handleBindingId?: string): Promise<number> {
    return (await this.listOutcomes(handleBindingId)).length;
  }

  async listOutcomes(handleBindingId?: string): Promise<StoredLocalOperationOutcome[]> {
    const outcomes = await this.storage.list<StoredLocalOperationOutcome>(outcomeStore);
    return handleBindingId
      ? outcomes.filter((outcome) => outcome.handleBindingId === handleBindingId)
      : outcomes;
  }

  async loadOutcome(key: string): Promise<StoredLocalOperationOutcome | null> {
    return this.storage.read<StoredLocalOperationOutcome>(outcomeStore, key);
  }

  async saveOutcome(outcome: StoredLocalOperationOutcome): Promise<void> {
    await this.storage.write(outcomeStore, outcome);
  }

  async removeOutcome(key: string): Promise<void> {
    await this.storage.remove(outcomeStore, key);
  }

  async loadAudioArtifactReceipt(
    generationId: string,
    artifactRole: AudioArtifactRole
  ): Promise<AudioArtifactReceipt | null> {
    return this.storage.read<AudioArtifactReceipt>(
      audioArtifactReceiptStore,
      audioArtifactReceiptKey(generationId, artifactRole)
    );
  }

  async saveAudioArtifactReceipt(receipt: AudioArtifactReceipt): Promise<void> {
    const expectedKey = audioArtifactReceiptKey(
      receipt.generationId,
      receipt.artifactRole
    );
    if (receipt.key !== expectedKey) {
      throw new Error("Audio artifact receipt key does not match its generation and role.");
    }
    await this.storage.write(audioArtifactReceiptStore, receipt);
  }

  async listAudioArtifactReceipts(): Promise<AudioArtifactReceipt[]> {
    return this.storage.list<AudioArtifactReceipt>(audioArtifactReceiptStore);
  }

  async removeAudioArtifactReceipt(
    generationId: string,
    artifactRole: AudioArtifactRole
  ): Promise<void> {
    await this.storage.remove(
      audioArtifactReceiptStore,
      audioArtifactReceiptKey(generationId, artifactRole)
    );
  }
}
