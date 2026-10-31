//! Agent one-click configuration write surface.
//!
//! The request contract and persistence rules mirror `wb_agents.py`: first
//! integration bytes are backed up, all target files are staged before write,
//! and restore returns the original bytes or removes files we created.

mod clients;
mod model_picker;
mod ops;
mod request;
mod store;
#[cfg(test)]
mod tests;
mod toml_env;
mod yaml;
#[cfg(test)]
mod yaml_tests;

pub use request::handle;
