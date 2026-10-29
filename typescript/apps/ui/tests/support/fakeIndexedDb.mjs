/**
 * Just enough IndexedDB for frameStore.ts under Node: object stores with a key
 * path, get/getAll/put/delete over keys and IDBKeyRange.bound, and transactions
 * that commit all-or-nothing on the next macrotask.
 *
 * `failPut(store, value)` returning an error aborts the transaction that put
 * it, the way a full disk aborts a real one with QuotaExceededError.
 */
function rank(key) {
  if (typeof key === "number") return 0;
  if (typeof key === "string") return 1;
  return 2;
}

function compare(a, b) {
  if (rank(a) !== rank(b)) return rank(a) - rank(b);
  if (!Array.isArray(a)) return a < b ? -1 : a > b ? 1 : 0;
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    const order = compare(a[i], b[i]);
    if (order !== 0) return order;
  }
  return a.length - b.length;
}

class FakeKeyRange {
  constructor(lower, upper) {
    this.lower = lower;
    this.upper = upper;
  }
  static bound(lower, upper) {
    return new FakeKeyRange(lower, upper);
  }
  includes(key) {
    return compare(this.lower, key) <= 0 && compare(key, this.upper) <= 0;
  }
}

const matches = (query, key) =>
  query instanceof FakeKeyRange ? query.includes(key) : compare(query, key) === 0;

export function installFakeIndexedDb({ failPut = () => null } = {}) {
  /** store name -> { keyPath, rows: Map<string, { key, value }> } */
  const stores = new Map();
  const options = { failPut };
  const keyOf = (keyPath, value) =>
    Array.isArray(keyPath) ? keyPath.map((part) => value[part]) : value[keyPath];

  function transaction(names, _mode) {
    const writes = [];
    const requests = [];
    let error = null;
    const tx = { oncomplete: null, onerror: null, onabort: null, error: null };
    const request = (run) => {
      const req = { onsuccess: null, onerror: null, result: undefined };
      requests.push(() => {
        req.result = run();
      });
      requests.at(-1).req = req;
      return req;
    };
    tx.objectStore = (name) => {
      if (![names].flat().includes(name)) throw new Error(`${name} is not in this transaction`);
      const store = stores.get(name);
      const sorted = () => [...store.rows.values()].sort((a, b) => compare(a.key, b.key));
      return {
        get: (key) => request(() => sorted().find((row) => matches(key, row.key))?.value),
        getAll: (query) =>
          request(() =>
            sorted()
              .filter((row) => query === undefined || matches(query, row.key))
              .map((row) => structuredClone(row.value))
          ),
        put(value) {
          const copy = structuredClone(value);
          error ??= options.failPut(name, copy);
          writes.push(() => store.rows.set(JSON.stringify(keyOf(store.keyPath, copy)), {
            key: keyOf(store.keyPath, copy),
            value: copy
          }));
          return request(() => undefined);
        },
        delete(query) {
          writes.push(() => {
            for (const [id, row] of store.rows) if (matches(query, row.key)) store.rows.delete(id);
          });
          return request(() => undefined);
        }
      };
    };
    setTimeout(() => {
      if (error) {
        tx.error = error;
        tx.onerror?.();
        tx.onabort?.();
        return;
      }
      for (const run of requests) {
        run();
        run.req.onsuccess?.();
      }
      for (const write of writes) write();
      tx.oncomplete?.();
    }, 0);
    return tx;
  }

  const db = {
    close() {},
    createObjectStore(name, { keyPath }) {
      stores.set(name, { keyPath, rows: new Map() });
    },
    transaction
  };

  globalThis.IDBKeyRange = FakeKeyRange;
  globalThis.indexedDB = {
    open() {
      const req = { result: db, onupgradeneeded: null, onsuccess: null, onerror: null };
      setTimeout(() => {
        if (stores.size === 0) req.onupgradeneeded?.();
        req.onsuccess?.();
      }, 0);
      return req;
    }
  };
  return { stores, options };
}
