"""Build-only correction for CodeArts catalog display names vs registered IDs.

Go overlays leave the external source checkout untouched. Unknown source shapes
fail explicitly instead of silently applying a partial protocol change.
"""
import json
from pathlib import Path


def _replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Unsupported CodeArts source shape for model ID correction")
    return text.replace(old, new, 1)


def model_id_overlay(source, directory):
    source, directory = Path(source).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    models = source / "internal/upstream/models.go"
    text = models.read_text(encoding="utf-8")
    text = _replace(text,
        'id := firstNonEmpty(m.ModelName, m.ModelID, m.ModelAlias, m.Params.ModelID)',
        'id := firstNonEmpty(m.Params.ModelID, m.ModelID, m.ModelAlias, m.ModelName)')
    text = _replace(text, 'Name:          id,', 'Name:          firstNonEmpty(m.ModelName, id),')
    text += r"""
// Resolve in the selected account's catalog; never share display aliases across accounts.
func RegisteredModelForAccount(accountID, requested string) string {
    infos, ok := AccountModels(accountID)
    if !ok { return requested }
    for _, model := range infos { if model.ID == requested { return model.ID } }
    resolve := func(match func(ModelInfo) bool) (string, bool) {
        found := ""
        for _, model := range infos {
            if !match(model) { continue }
            if found != "" && found != model.ID { return requested, true }
            found = model.ID
        }
        return found, found != ""
    }
    for _, match := range []func(ModelInfo) bool {
        func(m ModelInfo) bool { return m.Name == requested },
        func(m ModelInfo) bool { return strings.EqualFold(m.ID, requested) },
        func(m ModelInfo) bool { return strings.EqualFold(m.Name, requested) },
    } {
        if id, matched := resolve(match); matched { return id }
    }
    return requested
}

func registeredChatBody(accountID, chatID string, messages []ChatMessage, model string, opts ChatOptions) map[string]any {
    body := chatBodyV2(chatID, messages, model, opts)
    body["model"] = RegisteredModelForAccount(accountID, model)
    return body
}

func (c *Client) ChatStreamForAccount(ctx context.Context, accountID, chatID string, messages []ChatMessage, traceID string, cred SignCredential, userName, model string, opts ChatOptions, benefit bool) (io.ReadCloser, error) {
    body := registeredChatBody(accountID, chatID, messages, model, opts)
    return c.sendChatV2(ctx, body, traceID, cred, cred.SecurityToken, chatID, sessionIDFor(chatID), benefit)
}
"""
    handler = source / "internal/server/handler.go"
    server = _replace(handler.read_text(encoding="utf-8"),
        '\t\taddAlias(mi.ID, mi.ContextWindow, mi.MaxTokens, mi.Benefit)',
        '\t\taddAlias(mi.ID, mi.ContextWindow, mi.MaxTokens, mi.Benefit)\n'
        '\t\tif mi.Name != "" && mi.Name != mi.ID {\n'
        '\t\t\tadd(mi.Name, mi.ContextWindow, mi.MaxTokens, mi.Benefit)\n\t\t}')
    server = _replace(server, '\tif h.modelKnownUnusable(model) {',
        '\tif h.cfg.AuthDir != "" { h.discoverMissingCatalogs(nil) } // Managed deployments load account-scoped IDs on cold requests.\n\tif h.modelKnownUnusable(model) {')
    server = _replace(server,
        'acct.Client.ChatStreamWithOptions(r.Context(), chatID, msgs, "", cred, acct.UserName, model, chatOpts, benefit)',
        'acct.Client.ChatStreamForAccount(r.Context(), acct.UID, chatID, msgs, "", cred, acct.UserName, model, chatOpts, benefit)')
    server = _replace(server, 'benefit := upstream.IsBenefitModel(acct.UID, model)',
        'benefit := upstream.IsBenefitModel(acct.UID, upstream.RegisteredModelForAccount(acct.UID, model))')
    availability = source / "internal/server/availability.go"
    probes = _replace(availability.read_text(encoding="utf-8"),
        '"model":    upstream.CanonicalModel(model),',
        '"model":    upstream.RegisteredModelForAccount(acct.UID, model),')
    probes = _replace(probes, 'upstream.IsBenefitModel(acct.UID, model))',
        'upstream.IsBenefitModel(acct.UID, upstream.RegisteredModelForAccount(acct.UID, model)))')
    replacements = {}
    for name, original, body in (("models.go", models, text), ("handler.go", handler, server),
                                  ("availability.go", availability, probes)):
        output = directory / name
        output.write_text(body, encoding="utf-8")
        replacements[str(original)] = str(output)
    overlay = directory / "overlay.json"
    overlay.write_text(json.dumps({"Replace": replacements}), encoding="utf-8")
    return overlay
