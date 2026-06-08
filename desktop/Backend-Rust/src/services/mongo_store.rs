//! MongoDB data store — the Rust side of the dataset shared with the Python
//! backend's `MongoFirestore` shim (`backend/database/mongo_firestore.py`).
//!
//! Storage model (path-faithful, no per-collection knowledge): a document at the
//! Firestore-style path `users/abc/action_items/123` is stored in Mongo
//! collection `action_items` as:
//! ```text
//! { "_id": "users/abc/action_items/123",   // full path
//!   "_p":  "users/abc/action_items",        // parent collection path
//!   "_k":  "123",                           // leaf doc id
//!   ...user fields }
//! ```
//! A collection query filters on `_p`. This matches the Python shim exactly so
//! both backends read and write one dataset.
//!
//! NOTE: wired into `FirestoreService` and the live data functions in Phase 2b;
//! this module is the reusable storage core.
#![allow(dead_code)]

use bson::{doc, Bson, Document};
use futures::stream::TryStreamExt;
use mongodb::{Client, Collection, Database};

/// Decompose a path into `(collection_name, parent_path, leaf_id)`, mirroring the
/// Python shim's `DocumentReference`: collection = 2nd-to-last segment,
/// parent = all but the last segment, id = last segment.
pub fn path_parts(path: &str) -> (String, String, String) {
    let segs: Vec<&str> = path.split('/').collect();
    let leaf_id = segs.last().copied().unwrap_or("").to_string();
    let parent = if segs.len() > 1 {
        segs[..segs.len() - 1].join("/")
    } else {
        String::new()
    };
    let coll = if segs.len() >= 2 {
        safe_collection(segs[segs.len() - 2])
    } else {
        safe_collection(&leaf_id)
    };
    (coll, parent, leaf_id)
}

/// Mongo collection names cannot contain `$` or spaces (matches Python `_safe`).
pub fn safe_collection(name: &str) -> String {
    name.replace('$', "_").replace(' ', "_")
}

/// Leaf collection name for a *collection* path,
/// e.g. `users/abc/action_items` -> `action_items`.
fn collection_of(parent_path: &str) -> String {
    safe_collection(parent_path.rsplit('/').next().unwrap_or(parent_path))
}

/// Thin MongoDB handle exposing the path-faithful document operations the live
/// data functions need. Cheap to clone (wraps an `Arc` client internally).
#[derive(Clone)]
pub struct MongoStore {
    db: Database,
}

impl MongoStore {
    /// Connect to MongoDB. `Client::with_uri_str` resolves lazily, so this
    /// succeeds even if the server is momentarily unreachable — the first
    /// operation surfaces a connection error instead.
    pub async fn connect(url: &str, db_name: &str) -> mongodb::error::Result<Self> {
        let client = Client::with_uri_str(url).await?;
        Ok(Self {
            db: client.database(db_name),
        })
    }

    /// Escape hatch for callers that need raw collection access.
    pub fn database(&self) -> &Database {
        &self.db
    }

    fn coll(&self, name: &str) -> Collection<Document> {
        self.db.collection::<Document>(name)
    }

    /// Firestore `.set()` without merge: replace the whole document with the base
    /// path fields plus `fields`. Upserts.
    pub async fn set(&self, path: &str, mut fields: Document) -> mongodb::error::Result<()> {
        let (coll, parent, id) = path_parts(path);
        fields.insert("_id", path);
        fields.insert("_p", parent);
        fields.insert("_k", id);
        self.coll(&coll)
            .replace_one(doc! {"_id": path}, fields)
            .upsert(true)
            .await?;
        Ok(())
    }

    /// Firestore `.set(merge=True)` / `.update()`: apply a Mongo update document
    /// (with `$set`/`$inc`/`$addToSet`/`$unset`), always stamping `_id`/`_p`/`_k`
    /// into `$set`. `upsert` mirrors merge-set (true) vs update (false).
    pub async fn apply(&self, path: &str, mut update: Document, upsert: bool) -> mongodb::error::Result<()> {
        let (coll, parent, id) = path_parts(path);
        let mut set_doc = match update.remove("$set") {
            Some(Bson::Document(d)) => d,
            _ => Document::new(),
        };
        set_doc.insert("_id", path);
        set_doc.insert("_p", parent);
        set_doc.insert("_k", id);
        update.insert("$set", set_doc);
        self.coll(&coll)
            .update_one(doc! {"_id": path}, update)
            .upsert(upsert)
            .await?;
        Ok(())
    }

    /// Fetch a single document by path (`None` if absent).
    pub async fn get(&self, path: &str) -> mongodb::error::Result<Option<Document>> {
        let (coll, _, _) = path_parts(path);
        self.coll(&coll).find_one(doc! {"_id": path}).await
    }

    /// Delete a single document by path.
    pub async fn delete(&self, path: &str) -> mongodb::error::Result<()> {
        let (coll, _, _) = path_parts(path);
        self.coll(&coll).delete_one(doc! {"_id": path}).await?;
        Ok(())
    }

    /// Query the documents in a collection (scoped by parent path via `_p`), with
    /// optional extra match clauses, sort, offset and limit.
    pub async fn query(
        &self,
        parent_path: &str,
        extra: Document,
        sort: Option<Document>,
        offset: u64,
        limit: Option<i64>,
    ) -> mongodb::error::Result<Vec<Document>> {
        let coll = collection_of(parent_path);
        let mut filter = doc! {"_p": parent_path};
        for (k, v) in extra {
            filter.insert(k, v);
        }
        let handle = self.coll(&coll);
        let mut find = handle.find(filter);
        if let Some(s) = sort {
            find = find.sort(s);
        }
        if offset > 0 {
            find = find.skip(offset);
        }
        if let Some(l) = limit {
            find = find.limit(l);
        }
        let cursor = find.await?;
        cursor.try_collect().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_parts_nested_subcollection() {
        let (coll, parent, id) = path_parts("users/abc/action_items/123");
        assert_eq!(coll, "action_items");
        assert_eq!(parent, "users/abc/action_items");
        assert_eq!(id, "123");
    }

    #[test]
    fn path_parts_top_level_user_doc() {
        // Matches Python: parts=["users","abc"] -> coll=parts[-2]="users",
        // parent="users", id="abc".
        let (coll, parent, id) = path_parts("users/abc");
        assert_eq!(coll, "users");
        assert_eq!(parent, "users");
        assert_eq!(id, "abc");
    }

    #[test]
    fn path_parts_date_keyed_doc() {
        let (coll, parent, id) = path_parts("users/abc/llm_usage/2026-06-08");
        assert_eq!(coll, "llm_usage");
        assert_eq!(parent, "users/abc/llm_usage");
        assert_eq!(id, "2026-06-08");
    }

    #[test]
    fn safe_collection_strips_dollar_and_space() {
        assert_eq!(safe_collection("a b$c"), "a_b_c");
        assert_eq!(safe_collection("action_items"), "action_items");
    }

    #[test]
    fn collection_of_returns_leaf() {
        assert_eq!(collection_of("users/abc/action_items"), "action_items");
        assert_eq!(collection_of("users"), "users");
        assert_eq!(collection_of("desktop_releases"), "desktop_releases");
    }

    /// Round-trip against a real MongoDB. Ignored by default (CI has no Mongo);
    /// run with `MONGO_TEST_URL=mongodb://localhost:27018 cargo test -- --ignored mongo_roundtrip`.
    #[tokio::test]
    #[ignore]
    async fn mongo_roundtrip_matches_shim_format() {
        let url = std::env::var("MONGO_TEST_URL").expect("set MONGO_TEST_URL");
        let store = MongoStore::connect(&url, "omi_store_test").await.unwrap();
        let path = "users/u1/action_items/a1";

        // Clean slate.
        store.delete(path).await.unwrap();

        // set() writes base path fields + user fields.
        store
            .set(path, doc! {"description": "buy milk", "completed": false})
            .await
            .unwrap();

        // Stored document carries the shim's reserved fields (_id/_p/_k).
        let got = store.get(path).await.unwrap().expect("doc exists");
        assert_eq!(got.get_str("_id").unwrap(), path);
        assert_eq!(got.get_str("_p").unwrap(), "users/u1/action_items");
        assert_eq!(got.get_str("_k").unwrap(), "a1");
        assert_eq!(got.get_str("description").unwrap(), "buy milk");
        assert_eq!(got.get_bool("completed").unwrap(), false);

        // query() scopes by parent path and applies extra match clauses.
        let rows = store
            .query("users/u1/action_items", doc! {"completed": false}, None, 0, None)
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].get_str("_k").unwrap(), "a1");

        // apply() merges with $set/$inc, preserving _p/_k.
        store
            .apply(path, doc! {"$set": {"completed": true}, "$inc": {"edits": 1}}, true)
            .await
            .unwrap();
        let got = store.get(path).await.unwrap().unwrap();
        assert_eq!(got.get_bool("completed").unwrap(), true);
        assert_eq!(got.get_i32("edits").unwrap(), 1);
        assert_eq!(got.get_str("_k").unwrap(), "a1"); // reserved fields intact

        // A non-matching query returns nothing.
        let none = store
            .query("users/u1/action_items", doc! {"completed": false}, None, 0, None)
            .await
            .unwrap();
        assert!(none.is_empty());

        // delete() removes it.
        store.delete(path).await.unwrap();
        assert!(store.get(path).await.unwrap().is_none());
    }
}
