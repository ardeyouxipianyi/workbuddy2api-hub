package server

import (
	"codearts2api/internal/upstream"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestBridgeImageWireAndFingerprint(t *testing.T) {
	body := []byte(`{"model":"vision-real-id","messages":[{"role":"user","content":[{"type":"text","text":"colour?"},{"type":"image_url","image_url":{"url":"data:image/png;base64,aGVsbG8=","detail":"high"}}]}]}`)
	req, err := parseChatRequest(body)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(buildUpstreamMessages(req, false))
	if !strings.Contains(string(raw), `"image_url"`) || !strings.Contains(string(raw), `"detail":"high"`) {
		t.Fatalf("image lost: %s", raw)
	}
	other, _ := parseChatRequest([]byte(strings.Replace(string(body), "aGVsbG8=", "d29ybGQ=", 1)))
	if fingerprintOf(req.Messages) == fingerprintOf(other.Messages) {
		t.Fatal("different images share conversation fingerprint")
	}
	reordered, _ := parseChatRequest([]byte(strings.Replace(string(body), `"url":"data:image/png;base64,aGVsbG8=","detail":"high"`, `"detail":"high","url":"data:image/png;base64,aGVsbG8="`, 1)))
	if fingerprintOf(req.Messages) != fingerprintOf(reordered.Messages) {
		t.Fatal("equivalent image key order changed fingerprint")
	}
}

func TestBridgeRejectUnsupportedMedia(t *testing.T) {
	for _, part := range []string{`{"type":"input_audio","input_audio":{"data":"x","format":"wav"}}`, `{"type":"video_url","video_url":{"url":"https://example.test/x.mp4"}}`, `{"type":"image_url","image_url":{"url":"file:///private.png"}}`, `{"type":"image_url","image_url":{"url":"https://example.test/x.png","detail":"invalid"}}`} {
		_, err := parseChatRequest([]byte(`{"model":"vision","messages":[{"role":"user","content":[` + part + `]}]}`))
		if err == nil {
			t.Fatalf("unsupported media accepted: %s", part)
		}
	}
}

func TestBridgeModelCapabilityUnknownNotFalse(t *testing.T) {
	yes, no := true, false
	entries := modelEntries([]upstream.ModelInfo{{ID: "vision-real-id", Name: "Vision", SupportsImages: &yes}, {ID: "text-id", SupportsImages: &no}, {ID: "unknown-id"}})
	found := map[string]map[string]any{}
	for _, e := range entries {
		found[e["id"].(string)] = e
	}
	if found["vision-real-id"]["supports_images"] != true || found["Vision"]["supports_images"] != true {
		t.Fatal("explicit capability lost")
	}
	if found["text-id"]["supports_images"] != false {
		t.Fatal("explicit false lost")
	}
	if _, ok := found["unknown-id"]["supports_images"]; ok {
		t.Fatal("unknown fabricated")
	}
	if _, ok := found["Qwen3-VL-235B"]["supports_images"]; ok {
		t.Fatal("seed must not claim vision")
	}
}

func TestBridgeModelMetadataExactIDAndAmbiguity(t *testing.T) {
	yes, no := true, false
	infos := []upstream.ModelInfo{{ID: "a-real", Name: "b-real", SupportsImages: &yes, ContextWindow: 200000, MaxTokens: 32000, Benefit: true}, {ID: "b-real", Name: "B", SupportsImages: &no, ContextWindow: 4096, MaxTokens: 1024}}
	for _, entry := range modelEntries(infos) {
		if entry["id"] == "b-real" && (entry["registered_id"] != "b-real" || entry["supports_images"] != false || entry["context_length"] != int64(4096) || entry["max_output_tokens"] != int64(1024) || entry["benefit"] == true) {
			t.Fatal("exact ID metadata collided", entry)
		}
	}
	infos = []upstream.ModelInfo{{ID: "a", Name: "Shared", SupportsImages: &yes}, {ID: "b", Name: "Shared", SupportsImages: &no}}
	for _, entry := range modelEntries(infos) {
		if entry["id"] == "Shared" {
			if _, ok := entry["supports_images"]; ok {
				t.Fatal("ambiguous alias claimed capability")
			}
		}
	}
}
func TestBridgeAudioOutputRejected(t *testing.T) {
	for _, fields := range []string{`"modalities":["text","audio"]`, `"audio":{"format":"wav"}`} {
		_, err := parseChatRequest([]byte(`{"model":"x",` + fields + `,"messages":[{"role":"user","content":"hi"}]}`))
		if err == nil {
			t.Fatal("audio output silently accepted")
		}
	}
}

func TestBridgeNonMultimodalErrorDoesNotCoolTextAccounts(t *testing.T) {
	for _, kind := range []string{"http", "first_sse", "late_sse"} {
		for _, stream := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/stream=%t", kind, stream), func(t *testing.T) {
				var attempts atomic.Int32
				var fail atomic.Bool
				fail.Store(true)
				backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					attempts.Add(1)
					if !fail.Load() {
						w.Header().Set("Content-Type", "text/event-stream")
						_, _ = io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n")
						return
					}
					if kind == "http" {
						w.WriteHeader(http.StatusBadRequest)
						_, _ = io.WriteString(w, `{"error":"InferHub.001001020.406 The request model is not multimodal"}`)
						return
					}
					w.Header().Set("Content-Type", "text/event-stream")
					if kind == "late_sse" {
						_, _ = io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n")
					}
					_, _ = io.WriteString(w, "event: done\ndata: {\"error_code\":\"InferHub.001001020.406\",\"error_msg\":\"The request model is not multimodal\"}\n\n")
				}))
				defer backend.Close()
				h, p := toolChoiceTestHandler(t, backend.URL)
				payload := fmt.Sprintf(`{"messages":[{"role":"user","content":"hi"}],"stream":%t}`, stream)
				for i := 0; i < 3; i++ {
					rec := httptest.NewRecorder()
					h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(payload)))
					wantStatus := http.StatusBadRequest
					if kind == "late_sse" && stream {
						wantStatus = http.StatusOK // The stream headers were already sent.
						if !strings.Contains(rec.Body.String(), "event: error") || !strings.Contains(rec.Body.String(), "data: [DONE]") {
							t.Fatalf("late error did not terminate the stream: %s", rec.Body.String())
						}
					}
					if rec.Code != wantStatus || !strings.Contains(rec.Body.String(), "InferHub.001001020.406") {
						t.Fatalf("status=%d want=%d body=%s", rec.Code, wantStatus, rec.Body.String())
					}
					if attempts.Load() != int32(i+1) {
						t.Fatalf("parameter error rotated or retried accounts: attempts=%d requests=%d", attempts.Load(), i+1)
					}
					assertToolChoicePoolHealthy(t, p)
				}
				fail.Store(false)
				rec := httptest.NewRecorder()
				h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"messages":[{"role":"user","content":"normal request"}]} `)))
				if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"content":"ok"`) {
					t.Fatalf("normal request failed after parameter errors: status=%d body=%s", rec.Code, rec.Body.String())
				}
				assertToolChoicePoolHealthy(t, p)
			})
		}
	}
}
