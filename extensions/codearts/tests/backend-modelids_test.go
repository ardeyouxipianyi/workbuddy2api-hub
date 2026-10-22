package upstream

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestBridgeRegisteredModelIDOverDisplayName(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == EpAgentList {
			w.Write([]byte(`{"agents":[{"agent_id":"test","is_primary_agent":true}]}`))
			return
		}
		w.Write([]byte(`{"gpts":{"models":[{"model_name":"GLM-5.3","model_alias":"glm-5.3","model_parameters":{"model_id":"glm-5.3","context_window":1048576,"max_tokens":65536}},{"model_name":"OpenPangu-2.0-Pro","model_alias":"openpangu-2.0-pro","model_parameters":{"model_id":"openpangu-2.0-pro"}},{"model_name":"GLM-5.2-ArkTS-SPARK","model_alias":"glm-5.2-sft-harmony","model_parameters":{"model_id":"glm-5.2-sft-harmony"}}]}}`))
	}))
	defer server.Close()
	c := New(time.Second)
	c.snapBase = server.URL
	models, err := c.fetchAgentModels(SignCredential{})
	if err != nil {
		t.Fatal(err)
	}
	if len(models) != 3 || models[0].ID != "glm-5.3" || models[0].Name != "GLM-5.3" {
		t.Fatalf("registered ID/display name lost: %+v", models)
	}
	SetAccountModels("bridge-model-test", models)
	for _, pair := range [][2]string{{"GLM-5.3", "glm-5.3"}, {"OpenPangu-2.0-Pro", "openpangu-2.0-pro"}, {"GLM-5.2-ArkTS-SPARK", "glm-5.2-sft-harmony"}} {
		if got := registeredChatBody("bridge-model-test", "", nil, pair[0], ChatOptions{})["model"]; got != pair[1] {
			t.Errorf("%s sent as %v, want %s", pair[0], got, pair[1])
		}
	}
	if RegisteredModelForAccount("bridge-model-test", "unknown-Exact-ID") != "unknown-Exact-ID" {
		t.Fatal("unknown ID was rewritten")
	}
}

func TestBridgeModelAliasesAreAccountScoped(t *testing.T) {
	SetAccountModels("scope-a", []ModelInfo{{ID: "exact-a", Name: "Shared Display"}})
	SetAccountModels("scope-b", []ModelInfo{{ID: "exact-b", Name: "Shared Display"}, {ID: "other-b", Name: "exact-a"}})
	if RegisteredModelForAccount("scope-a", "Shared Display") != "exact-a" {
		t.Fatal("account a alias polluted")
	}
	if RegisteredModelForAccount("scope-b", "Shared Display") != "exact-b" {
		t.Fatal("account b alias lost")
	}
	if RegisteredModelForAccount("scope-a", "exact-a") != "exact-a" {
		t.Fatal("another display overrode exact ID")
	}
	if RegisteredModelForAccount("missing-account", "Shared Display") != "Shared Display" {
		t.Fatal("cross-account fallback")
	}
}

func TestBridgeAmbiguousDisplayAndBenefitAlias(t *testing.T) {
	SetAccountModels("ambiguous", []ModelInfo{{ID: "model-a", Name: "Same Name"}, {ID: "model-b", Name: "Same Name"}})
	if RegisteredModelForAccount("ambiguous", "Same Name") != "Same Name" {
		t.Fatal("ambiguous name guessed")
	}
	SetAccountModels("benefit-alias", []ModelInfo{{ID: "free-model", Name: "Free Display", Benefit: true}})
	resolved := RegisteredModelForAccount("benefit-alias", "Free Display")
	if resolved != "free-model" || !IsBenefitModel("benefit-alias", resolved) {
		t.Fatal("resolved ID lost benefit routing")
	}
}
