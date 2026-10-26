"""Image wire/capability corrections applied only during an opt-in Go build."""


def apply(source, models, server, replace):
    models = replace(models, 'type ModelInfo struct {', 'type ModelInfo struct {\n\tSupportsImages *bool `json:"supports_images,omitempty"`\n\tImagesConflict bool `json:"capability_conflict,omitempty"`')
    # Missing catalog metadata stays nil, including on static fallback seeds.
    models = models.replace('MaxTokens     int64  `json:"max_tokens"`', 'MaxTokens     int64  `json:"max_tokens"`\n\t\t\t\t\tSupportsImages *bool `json:"supports_images"`')
    if models.count('SupportsImages *bool `json:"supports_images"`') != 3:
        raise ValueError("Unsupported CodeArts model capability source shape")
    models = replace(models, 'Desc:          m.Params.ModelDesc,', 'Desc:          m.Params.ModelDesc,\n\t\t\tSupportsImages: m.Params.SupportsImages,')
    models = replace(models, 'MaxTokens: m.MaxTokens, Desc: m.ModelDesc})', 'MaxTokens: m.MaxTokens, Desc: m.ModelDesc, SupportsImages: m.SupportsImages})')
    models = replace(models, 'Benefit: true,', 'Benefit: true, SupportsImages: m.SupportsImages,')
    models = replace(models, '\tkeep.Benefit = benefit', '''\tkeep.Benefit = benefit
    keep.ImagesConflict = cur.ImagesConflict || next.ImagesConflict || (cur.SupportsImages != nil && next.SupportsImages != nil && *cur.SupportsImages != *next.SupportsImages)
    if keep.ImagesConflict { keep.SupportsImages = nil } else if keep.SupportsImages == nil { keep.SupportsImages = drop.SupportsImages }''')
    server = replace(server, '\treturn out\n}\n\n// fetchDynamicModels', '''    for _, entry := range out {
        id, _ := entry["id"].(string)
        mi := bridgeModelMetadata(infos, id)
        if mi == nil { continue }
        entry["registered_id"] = mi.ID
        entry["name"] = mi.Name
        delete(entry,"context_length"); delete(entry,"max_output_tokens"); delete(entry,"benefit")
        if mi.Benefit { entry["benefit"] = true }
        if mi.ContextWindow > 0 { entry["contextWindow"] = mi.ContextWindow; entry["context_length"] = mi.ContextWindow }
        if mi.MaxTokens > 0 { entry["maxTokens"] = mi.MaxTokens; entry["max_output_tokens"] = mi.MaxTokens }
        if mi.ImagesConflict { entry["capability_conflict"] = true }
        if mi.SupportsImages != nil && !mi.ImagesConflict {
            entry["supports_images"] = *mi.SupportsImages
            entry["capability_source"] = "account_catalog"
            if *mi.SupportsImages { entry["input_modalities"] = []string{"text","image"} } else { entry["input_modalities"] = []string{"text"} }
            entry["output_modalities"] = []string{"text"}
        }
    }
\treturn out
}

// fetchDynamicModels''')
    server += r'''

// Same priority/ambiguity rules as account-scoped chat ID resolution.
func bridgeModelMetadata(infos []upstream.ModelInfo, id string) *upstream.ModelInfo {
    for i := range infos { if infos[i].ID == id { return &infos[i] } }
    for _, match := range []func(upstream.ModelInfo) bool {
        func(m upstream.ModelInfo) bool { return m.Name == id },
        func(m upstream.ModelInfo) bool { return strings.EqualFold(m.ID,id) },
        func(m upstream.ModelInfo) bool { return strings.EqualFold(m.Name,id) },
    } {
        var found *upstream.ModelInfo
        for i := range infos {
            if !match(infos[i]) { continue }
            if found != nil && found.ID != infos[i].ID { return nil }
            found = &infos[i]
        }
        if found != nil { return found }
    }
    return nil
}
'''
    server = replace(server, 'Role: m.Role, Content: m.Text}', 'Role: m.Role, Content: m.wireContent()}')
    server = replace(server, 'Role: "assistant", Content: m.Text, ReasoningContent:', 'Role: "assistant", Content: m.wireContent(), ReasoningContent:')
    server = replace(server, 'if m.Text == "" {\n\t\t\t\t\twire.Content = nil', 'if m.Text == "" && len(m.ImageContent) == 0 {\n\t\t\t\t\twire.Content = nil')
    server = replace(server, 'Role: "tool", Content: content,', 'Role: "tool", Content: m.toolContent(content),')
    server = replace(server,
        'return strings.Contains(low, "inferhub.001001005") || strings.Contains(low, "request param is invalid")',
        'return strings.Contains(low, "inferhub.001001005") || strings.Contains(low, "request param is invalid") || strings.Contains(low, "inferhub.001001020.406")')
    request = (source / 'internal/server/request.go').read_text(encoding='utf-8')
    request = replace(request, '\t"strings"', '\t"strings"\n\t"net/url"\n\t"encoding/base64"')
    request = replace(request, 'type openAIMessage struct {', 'type openAIMessage struct {\n\tImageContent json.RawMessage')
    request = replace(request, '\tif len(probe.ToolCalls)', '''    imageContent, err := parseImageContent(probe.Content)
    if err != nil { return openAIMessage{}, err }
    m.ImageContent = imageContent
\tif len(probe.ToolCalls)''')
    request = replace(request, '\tsb.WriteString(m.Text)', '\tsb.WriteString(m.Text)\n\tif len(m.ImageContent)>0 { var v any; json.Unmarshal(m.ImageContent,&v); normalized,_:=json.Marshal(v); sum:=sha256.Sum256(normalized); sb.WriteString("\\x00image:"+hex.EncodeToString(sum[:])) }')
    request = replace(request, 'func parseChatRequest(body []byte) (*chatRequest, error) {', 'func parseChatRequest(body []byte) (*chatRequest, error) {\n    if err:=bridgeOutputModalities(body); err!=nil { return nil,err }')
    request += r'''
func bridgeOutputModalities(body []byte) error {
    var raw map[string]json.RawMessage
    if json.Unmarshal(body,&raw)!=nil { return nil }
    if audio,ok:=raw["audio"];ok && string(audio)!="null" { return fmt.Errorf("audio output is not supported") }
    if value,ok:=raw["modalities"];ok {
        var modes []string
        if json.Unmarshal(value,&modes)!=nil || len(modes)!=1 || modes[0]!="text" { return fmt.Errorf("only text output is supported") }
    }
    return nil
}


// Preserve image parts, including detail/order. Legacy text fingerprints stay stable.
func (m openAIMessage) wireContent() any {
    if len(m.ImageContent) > 0 { return m.ImageContent }
    return m.Text
}
func (m openAIMessage) toolContent(fallback string) any {
    if len(m.ImageContent) > 0 { return m.ImageContent }
    return fallback
}
func parseImageContent(raw json.RawMessage) (json.RawMessage, error) {
    if len(raw)==0 || string(raw)=="null" { return nil,nil }
    var text string
    if json.Unmarshal(raw,&text)==nil { return nil,nil }
    var parts []map[string]json.RawMessage
    if json.Unmarshal(raw,&parts)!=nil { return nil,fmt.Errorf("content must be text or an array of text/image_url parts") }
    hasImage:=false
    for _,p:=range parts {
        var kind string; json.Unmarshal(p["type"],&kind)
        switch kind {
        case "text":
            if json.Unmarshal(p["text"],&text)!=nil { return nil,fmt.Errorf("text part requires text") }
        case "image_url":
            var image struct { URL string `json:"url"`; Detail string `json:"detail"` }
            if json.Unmarshal(p["image_url"],&image)!=nil || image.URL=="" { return nil,fmt.Errorf("image_url requires a URL object") }
            if image.Detail!="" && image.Detail!="auto" && image.Detail!="low" && image.Detail!="high" { return nil,fmt.Errorf("image detail must be auto, low or high") }
            u,err:=url.Parse(image.URL)
            if err!=nil { return nil,fmt.Errorf("invalid image URL") }
            if u.Scheme=="data" {
                header,data,ok:=strings.Cut(image.URL,",")
                valid:=false
                for _,mime:=range []string{"image/png","image/jpeg","image/webp","image/gif"} { if header=="data:"+mime+";base64" { valid=true } }
                decoded,e:=base64.StdEncoding.DecodeString(data)
                if !ok || !valid || e!=nil || len(decoded)==0 || len(decoded)>4<<20 { return nil,fmt.Errorf("image data must be JPEG/PNG/WebP/GIF base64, at most 4 MiB") }
            } else if (u.Scheme!="http" && u.Scheme!="https") || u.Host=="" || u.User!=nil { return nil,fmt.Errorf("image URL must use HTTP(S) or an image data URL") }
            hasImage=true
        default:
            return nil,fmt.Errorf("unsupported content part %q: only text and image_url are supported; audio/video are not supported",kind)
        }
    }
    if hasImage { return append(json.RawMessage(nil),raw...),nil }
    return nil,nil
}
'''
    return models, server, request
