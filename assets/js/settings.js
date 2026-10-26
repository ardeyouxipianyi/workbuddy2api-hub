async function loadSettings(showToast){
  try {
    const data = await getJSON('/settings');
    const pwdState = document.getElementById('setPwdState');
    if(pwdState){
      pwdState.textContent = data.panel_password_is_default ? '(当前为默认密码)' : '(已自定义)';
      pwdState.style.color = data.panel_password_is_default ? 'var(--warn)' : 'var(--accent2)';
    }
    const keyState = document.getElementById('setKeyState');
    if(keyState){
      const count = (data.api_keys || []).filter(k => k.enabled).length;
      keyState.textContent = count ? ('(' + count + ' 个生效)') : '(当前不校验)';
      keyState.style.color = count ? 'var(--accent2)' : 'var(--dim)';
    }

    applyLimits(data);
    applyUiLanguageSelect(data.ui_language || 'zh');

    // 账号区折叠：服务端没存过、存坏了，或后端根本没这个字段，都按展开算。
    applyAccountsCollapsed(data.accounts_collapsed === true);
    // 「(切换前)」行开关同理：只有明确存过 true 才收起，其余都照常显示。
    applyKeyBeforeHidden(data.key_before_hidden === true);
    // 顶部页签显示：字段缺失或存坏了都按「全部显示」算，见 normaliseHiddenPages。
    applyHiddenPages(data.hidden_pages);
    // 账号页签模式：同样只有明确存过 true 才拆开，其余（没存过、存坏了、
    // 后端没这个字段）都保持上游那个合并的「网关与账号」页。
    const separateTab = data.accounts_separate_tab === true;
    applyAccountsTabLayout(separateTab);
    const separateState = document.getElementById('setAccountsSeparateState');
    if(separateState) separateState.textContent = separateTab ? '(已开启)' : '(默认：合并)';
    const separateInput = document.getElementById('setAccountsSeparate');
    if(separateInput) separateInput.checked = separateTab;

    // 面板窗口限高：字段缺失或存坏了都按默认 0（不限）算：区块随页面滚动，不出现双滚动条。
    // 坏值回落默认状态这条口径与「顶部页签显示」「账号区折叠」一致，见
    // wb_settings.section_max_height 与 applyWinMaxHeight。
    const rawWinMax = data.section_max_height;
    const winMax = (typeof rawWinMax === 'number' && Number.isFinite(rawWinMax) && rawWinMax >= 0)
      ? rawWinMax : 0;
    applyWinMaxHeight(winMax);
    const winMaxInput = document.getElementById('setWinMaxHeight');
    if(winMaxInput) winMaxInput.value = String(Math.round(winMax));

      const creditsHoursInput = document.getElementById('setCreditsRefreshHours');
      if(creditsHoursInput && data.credits_refresh_hours !== undefined){
        creditsHoursInput.value = String(Number(data.credits_refresh_hours || 0));
      }

      const pricingOn = data.pricing_enabled !== false;
      const pricingEnabledInput = document.getElementById('setPricingEnabled');
      if(pricingEnabledInput) pricingEnabledInput.checked = pricingOn;
      applyPricingEnabled(pricingOn);
      const pricingMinutes = Number(data.pricing_refresh_minutes || 0);
      const pricingInput = document.getElementById('setPricingMinutes');
      if(pricingInput) pricingInput.value = String(pricingMinutes);
      const pricingState = document.getElementById('setPricingState');
      if(pricingState){
        pricingState.textContent = pricingMinutes > 0 ? ('(每 ' + pricingMinutes + ' 分钟)') : '(未启用)';
        pricingState.style.color = pricingMinutes > 0 ? 'var(--accent2)' : 'var(--dim)';
      }
      const variantOn = data.pricing_variant_inherit !== false;
      const variantInput = document.getElementById('setPricingVariant');
      if(variantInput) variantInput.checked = variantOn;
      // 关闭时不去拉 /pricing：取价与未定价清单都没有意义，省掉这一趟。
      if(pricingOn){
        loadPricingStatus();
      } else {
        const meta = document.getElementById('setPricingMeta');
        if(meta) meta.textContent = '　价估算已关闭，未取价也不计费。';
        const cur = document.getElementById('setPricingCurrent');
        if(cur) cur.innerHTML = '';
        const gaps = document.getElementById('setPricingGaps');
        if(gaps) gaps.innerHTML = '';
      }
      const autoSwitch = data.auto_switch_product === true;
      const creditFallback = data.free_model_credit_fallback === true;
      const anthropicMsg = data.enable_anthropic_messages === true;
      const filterModelsByKey = data.filter_models_by_key === true;
      const reasoningEffortMap = data.reasoning_effort_map !== false;
      const reasoningEffortMapInput = document.getElementById('setReasoningEffortMap');
      if(reasoningEffortMapInput) reasoningEffortMapInput.checked = reasoningEffortMap;
      const reasoningEffortMapState = document.getElementById('setReasoningEffortMapState');
      if(reasoningEffortMapState){
        reasoningEffortMapState.textContent = reasoningEffortMap ? '(已启用)' : '(未启用)';
        reasoningEffortMapState.style.color = reasoningEffortMap ? 'var(--accent2)' : 'var(--dim)';
      }
      const filterModelsByKeyInput = document.getElementById('setFilterModelsByKey');
      if(filterModelsByKeyInput) filterModelsByKeyInput.checked = filterModelsByKey;
      const filterModelsByKeyState = document.getElementById('setFilterModelsByKeyState');
      if(filterModelsByKeyState){
        filterModelsByKeyState.textContent = filterModelsByKey ? '(已启用)' : '(未启用)';
        filterModelsByKeyState.style.color = filterModelsByKey ? 'var(--accent2)' : 'var(--dim)';
      }
      const modelsOnlyAvailable = data.models_only_available === true;
      const modelsOnlyAvailableInput = document.getElementById('setModelsOnlyAvailable');
      if(modelsOnlyAvailableInput) modelsOnlyAvailableInput.checked = modelsOnlyAvailable;
      const modelsOnlyAvailableState = document.getElementById('setModelsOnlyAvailableState');
      if(modelsOnlyAvailableState){
        modelsOnlyAvailableState.textContent = modelsOnlyAvailable ? '(已启用)' : '(未启用)';
        modelsOnlyAvailableState.style.color = modelsOnlyAvailable ? 'var(--accent2)' : 'var(--dim)';
      }
      const anthropicMsgInput = document.getElementById('setAnthropicMessages');
      if(anthropicMsgInput) anthropicMsgInput.checked = anthropicMsg;
      const anthropicMsgState = document.getElementById('setAnthropicMessagesState');
      if(anthropicMsgState){
        anthropicMsgState.textContent = anthropicMsg ? '(已启用)' : '(未启用)';
        anthropicMsgState.style.color = anthropicMsg ? 'var(--accent2)' : 'var(--dim)';
      }
      const creditFallbackInput = document.getElementById('setCreditFallback');
      if(creditFallbackInput) creditFallbackInput.checked = creditFallback;
      const creditFallbackState = document.getElementById('setCreditFallbackState');
      if(creditFallbackState){
        creditFallbackState.textContent = creditFallback ? '(已启用)' : '(未启用)';
        creditFallbackState.style.color = creditFallback ? 'var(--accent2)' : 'var(--dim)';
      }
      const autoSwitchInput = document.getElementById('setAutoSwitch');
      if(autoSwitchInput) autoSwitchInput.checked = autoSwitch;
      const autoSwitchState = document.getElementById('setAutoSwitchState');
      if(autoSwitchState){
        autoSwitchState.textContent = autoSwitch ? '(已启用)' : '(未启用)';
        autoSwitchState.style.color = autoSwitch ? 'var(--accent2)' : 'var(--dim)';
      }
      const autoSwitchProducts = Array.isArray(data.auto_switch_products)
        ? data.auto_switch_products : ['workbuddy', 'vscode', 'cli'];
      [['setAutoSwitchWB', 'workbuddy'], ['setAutoSwitchVSC', 'vscode'],
       ['setAutoSwitchCLI', 'cli']].forEach(function(pair){
        const input = document.getElementById(pair[0]);
        if(input) input.checked = autoSwitchProducts.indexOf(pair[1]) >= 0;
      });
      IDENTITY_VERSION_DEFAULTS = (data.identity_version_defaults && typeof data.identity_version_defaults === 'object')
        ? data.identity_version_defaults : {};
      IDENTITY_VERSION_CURRENT = (data.identity && data.identity.versions && typeof data.identity.versions === 'object')
        ? data.identity.versions : {};
      renderIdentityVersions();
      const privateData = (data.identity && data.identity.private_data) || 'auto';
      const privateDataInput = document.getElementById('setPrivateData');
      if(privateDataInput) privateDataInput.value = privateData;
      const privateDataState = document.getElementById('setPrivateDataState');
      if(privateDataState){
        privateDataState.textContent = privateData === 'auto'
          ? '(跟随身份默认)' : ('(' + privateData + ')');
        privateDataState.style.color = privateData === 'auto'
          ? 'var(--dim)' : 'var(--accent2)';
      }
      const dailyWeb = data.daily_chat_web !== false;
      const dailyWebInput = document.getElementById('setDailyChatWeb');
      if(dailyWebInput) dailyWebInput.checked = dailyWeb;
      const dailyWebState = document.getElementById('setDailyChatWebState');
      if(dailyWebState){
        dailyWebState.textContent = dailyWeb ? '(已启用)' : '(仅桌面端对话)';
        dailyWebState.style.color = dailyWeb ? 'var(--accent2)' : 'var(--dim)';
      }
      // ① 时段：显式时段优先；留空时后端每天随机抽两个（间隔 12 小时）。
      const scheduleCfg = (data.schedule && typeof data.schedule === 'object') ? data.schedule : {};
      const dailyChatTimes = Array.isArray(scheduleCfg.daily_chat) ? scheduleCfg.daily_chat : [];
      renderDailyChatTimes(dailyChatTimes);
      const dailyChatEffective = Array.isArray(scheduleCfg.daily_chat_effective)
        ? scheduleCfg.daily_chat_effective : [];
      const dailyChatEffectiveEl = document.getElementById('setDailyChatEffective');
      if(dailyChatEffectiveEl){
        dailyChatEffectiveEl.textContent = dailyChatEffective.length
          ? (dailyChatEffective.join(' / ') + (scheduleCfg.daily_chat_random ? '（随机）' : ''))
          : '-';
      }
      // ② 候选模型：有序，第一个是首选。
      renderDailyChatModelList(Array.isArray(data.daily_chat_models) ? data.daily_chat_models : []);
      // ③ 预算：单账号总预算、单轮上限、并发、建会话退避。
      const dailyChatBudgetDefault = Number(data.daily_chat_budget_default || 600);
      const dailyChatBudget = Number(data.daily_chat_budget != null
        ? data.daily_chat_budget : dailyChatBudgetDefault);
      const dailyChatBudgetInput = document.getElementById('setDailyChatBudget');
      if(dailyChatBudgetInput){
        dailyChatBudgetInput.value = String(dailyChatBudget);
        dailyChatBudgetInput.max = String(Number(data.daily_chat_budget_max || 7200));
      }
      const dailyChatTurnDefault = Number(data.daily_chat_web_timeout_default || 120);
      const dailyChatTurn = Number(data.daily_chat_web_timeout || dailyChatTurnDefault);
      const dailyChatTurnInput = document.getElementById('setDailyChatTurnTimeout');
      if(dailyChatTurnInput){
        dailyChatTurnInput.value = String(dailyChatTurn);
        dailyChatTurnInput.max = String(Number(data.daily_chat_web_timeout_max || 3600));
      }
      const dailyChatConcurrency = Number(data.daily_chat_web_concurrency || 1);
      const dailyChatConcurrencyInput = document.getElementById('setDailyChatWebConcurrency');
      if(dailyChatConcurrencyInput) dailyChatConcurrencyInput.value = String(dailyChatConcurrency);
      // 建会话阶段失败的退避：0 表示不退避，所以不能用 || 兜底。
      const dailyChatBackoffDefault = Number(
        data.daily_chat_web_retry_backoff_default != null
          ? data.daily_chat_web_retry_backoff_default : 5);
      const dailyChatBackoff = Number(
        data.daily_chat_web_retry_backoff != null
          ? data.daily_chat_web_retry_backoff : dailyChatBackoffDefault);
      const dailyChatBackoffInput = document.getElementById('setDailyChatWebRetryBackoff');
      if(dailyChatBackoffInput){
        dailyChatBackoffInput.value = String(dailyChatBackoff);
        dailyChatBackoffInput.max = String(Number(data.daily_chat_web_retry_backoff_max || 60));
      }
      const dailyChatCatchup = data.daily_chat_startup_catchup !== false;
      const dailyChatCatchupInput = document.getElementById('setDailyChatStartupCatchup');
      if(dailyChatCatchupInput) dailyChatCatchupInput.checked = dailyChatCatchup;
      const dailyChatCatchupState = document.getElementById('setDailyChatStartupCatchupState');
      if(dailyChatCatchupState){
        dailyChatCatchupState.textContent = dailyChatCatchup ? '(已启用)' : '(关闭)';
        dailyChatCatchupState.style.color = dailyChatCatchup ? 'var(--accent2)' : 'var(--dim)';
      }
      const dailyChatActivityFallback = data.daily_chat_web_fallback_activity === true;
      const dailyChatActivityFallbackInput = document.getElementById('setDailyChatActivityFallback');
      if(dailyChatActivityFallbackInput) dailyChatActivityFallbackInput.checked = dailyChatActivityFallback;
      const dailyChatActivityFallbackState = document.getElementById('setDailyChatActivityFallbackState');
      if(dailyChatActivityFallbackState){
        dailyChatActivityFallbackState.textContent = dailyChatActivityFallback ? '(已启用)' : '(关闭)';
        dailyChatActivityFallbackState.style.color = dailyChatActivityFallback ? 'var(--accent2)' : 'var(--dim)';
      }
      const dailyWebRandomPrompt = data.daily_chat_web_random_prompt === true;
      const dailyWebRandomPromptInput = document.getElementById('setDailyChatRandomPrompt');
      if(dailyWebRandomPromptInput) dailyWebRandomPromptInput.checked = dailyWebRandomPrompt;
      PROMPT_PICKER_BUILTIN = Array.isArray(data.daily_chat_web_builtin_prompts)
        ? data.daily_chat_web_builtin_prompts.slice() : [];
      window.DAILY_CHAT_PROMPTS_STORED = Array.isArray(data.daily_chat_web_prompts)
        ? data.daily_chat_web_prompts.slice() : [];
      updatePromptPickerState();
      const dailyWebRandomPromptState = document.getElementById('setDailyChatRandomPromptState');
      if(dailyWebRandomPromptState){
        dailyWebRandomPromptState.textContent = dailyWebRandomPrompt
          ? ('已启用 · ' + (window.DAILY_CHAT_PROMPTS_STORED.length
              ? window.DAILY_CHAT_PROMPTS_STORED.length + ' 条候选'
              : '默认全部内置 300 条'))
          : '(未启用 · 固定发 Hi)';
        dailyWebRandomPromptState.style.color = dailyWebRandomPrompt
          ? 'var(--accent2)' : 'var(--dim)';
      }
      const promptCfg = (data.prompt && typeof data.prompt === 'object') ? data.prompt : {};
      const promptMode = ['passthrough', 'append', 'custom'].indexOf(promptCfg.mode) >= 0
        ? promptCfg.mode : 'passthrough';
      const promptModeInput = document.getElementById('setGlobalPromptMode');
      if(promptModeInput) promptModeInput.value = promptMode;
      const promptTextInput = document.getElementById('setGlobalPromptText');
      if(promptTextInput) promptTextInput.value = promptCfg.text || '';
      const promptState = document.getElementById('setGlobalPromptState');
      if(promptState){
        promptState.textContent = promptMode === 'passthrough' ? '(透传)'
          : (promptMode === 'append' ? '(追加)' : '(替换)');
        promptState.style.color = promptMode === 'passthrough'
          ? 'var(--dim)' : 'var(--accent2)';
      }
      const localWeb = data.local_web_tools === true;
      const localWebInput = document.getElementById('setLocalWebTools');
      if(localWebInput) localWebInput.checked = localWeb;
      const localWebState = document.getElementById('setLocalWebToolsState');
      if(localWebState){
        localWebState.textContent = localWeb ? '(已启用)' : '(直通)';
        localWebState.style.color = localWeb ? 'var(--accent2)' : 'var(--dim)';
      }
      const remainingPriority = data.remaining_priority_enabled === true;
      const remainingPriorityInput = document.getElementById('setRemainingPriority');
      if(remainingPriorityInput) remainingPriorityInput.checked = remainingPriority;
      const remainingPriorityState = document.getElementById('setRemainingPriorityState');
      if(remainingPriorityState){
        remainingPriorityState.textContent = remainingPriority ? '(已启用)' : '(未启用)';
        remainingPriorityState.style.color = remainingPriority ? 'var(--accent2)' : 'var(--dim)';
      }
      const realmFallback = data.realm_fallback_enabled === true;
      const realmFallbackInput = document.getElementById('setRealmFallback');
      if(realmFallbackInput) realmFallbackInput.checked = realmFallback;
      const realmFallbackState = document.getElementById('setRealmFallbackState');
      if(realmFallbackState){
        realmFallbackState.textContent = realmFallback ? '(已启用)' : '(未启用)';
        realmFallbackState.style.color = realmFallback ? 'var(--accent2)' : 'var(--dim)';
      }
      const windowProbe = data.window_probe !== false;
      const windowProbeInput = document.getElementById('setWindowProbe');
      if(windowProbeInput) windowProbeInput.checked = windowProbe;
      const windowProbeState = document.getElementById('setWindowProbeState');
      if(windowProbeState){
        windowProbeState.textContent = windowProbe ? '(已启用)' : '(未启用)';
        windowProbeState.style.color = windowProbe ? 'var(--accent2)' : 'var(--dim)';
      }
      const egressBackoff = data.egress_backoff_enabled === true;
      const egressBackoffInput = document.getElementById('setEgressBackoff');
      if(egressBackoffInput) egressBackoffInput.checked = egressBackoff;
      const egressBackoffState = document.getElementById('setEgressBackoffState');
      if(egressBackoffState){
        egressBackoffState.textContent = egressBackoff ? '(已启用)' : '(未启用)';
        egressBackoffState.style.color = egressBackoff ? 'var(--accent2)' : 'var(--dim)';
      }
      const accountConcurrent = (typeof data.account_max_concurrent === 'number') ? data.account_max_concurrent : 0;
      const accountConcurrentInput = document.getElementById('setAccountConcurrent');
      if(accountConcurrentInput) accountConcurrentInput.value = accountConcurrent;
      const accountConcurrentState = document.getElementById('setAccountConcurrentState');
      if(accountConcurrentState){
        accountConcurrentState.textContent = accountConcurrent > 0 ? '(' + accountConcurrent + ' 个 / 账号)' : '(不限)';
        accountConcurrentState.style.color = accountConcurrent > 0 ? 'var(--accent2)' : 'var(--dim)';
      }
      const affinityStableKey = data.affinity_stable_key_enabled === true;
      const affinityStableKeyInput = document.getElementById('setAffinityStableKey');
      if(affinityStableKeyInput) affinityStableKeyInput.checked = affinityStableKey;
      const affinityStableKeyState = document.getElementById('setAffinityStableKeyState');
      if(affinityStableKeyState){
        affinityStableKeyState.textContent = affinityStableKey ? '(已启用)' : '(未启用)';
        affinityStableKeyState.style.color = affinityStableKey ? 'var(--accent2)' : 'var(--dim)';
      }
      SAVED_QUOTA_SCAN_MODELS = Array.isArray(data.quota_scan_models) ? data.quota_scan_models.slice() : [];
      QUOTA_SCAN_SELECTED = SAVED_QUOTA_SCAN_MODELS.slice();
      await loadQuotaScanCatalog();
      renderQuotaScanModels();
      renderQuotaScanState(data);
      // 上游响应头超时：没改过时后端给的就是默认 120，所以「等于 120」按默认态
      // 显示，只有被改过才标出实际秒数。
      const headerTimeout = (data.upstream || {}).header_timeout_seconds;
      const headerTimeoutInput = document.getElementById('setHeaderTimeout');
      if(headerTimeoutInput){
        headerTimeoutInput.value = (typeof headerTimeout === 'number') ? headerTimeout : 120;
      }
      const headerTimeoutState = document.getElementById('setHeaderTimeoutState');
      if(headerTimeoutState){
        const headerTimeoutDefault = (typeof headerTimeout !== 'number') || headerTimeout === 120;
        headerTimeoutState.textContent = headerTimeoutDefault ? '(默认 120 秒)' : '(' + headerTimeout + ' 秒)';
        headerTimeoutState.style.color = headerTimeoutDefault ? 'var(--dim)' : 'var(--accent2)';
      }
      const logDbEnabled = data.log_db_enabled === true;
      const logDbEnabledInput = document.getElementById('setLogDbEnabled');
      if(logDbEnabledInput) logDbEnabledInput.checked = logDbEnabled;
      const logDbState = document.getElementById('setLogDbState');
      if(logDbState){
        logDbState.textContent = logDbEnabled ? '(已启用)' : '(未启用)';
        logDbState.style.color = logDbEnabled ? 'var(--accent2)' : 'var(--dim)';
      }
      fillCapInput('setLogDbMaxRows', data.log_db_max_rows);
      fillCapInput('setLogDbMaxDays', data.log_db_max_days);
      fillCapInputMB('setLogDbMaxMB', data.log_db_max_bytes);
    // Rebuild the editable rows, keeping stored values so an untouched field
    // is not accidentally blanked.
    API_KEY_ROWS = (data.api_keys || []).map(k => ({
      id: k.id || '',
      name: k.name || '',
      key: '',
      // The server keeps the stored value when a row comes back blank, so
      // leaving the field untouched will not wipe an existing key.
      stored_key: '',
      masked: k.masked || '',
      realm: k.realm || '',
      models: Array.isArray(k.models) ? k.models.slice() : [],
      accounts: Array.isArray(k.accounts) ? k.accounts.slice() : [],
      concurrency_limit: parseInt(k.concurrency_limit || 0, 10) || 0,
      enabled: k.enabled !== false,
      created_at: k.created_at || '',
      _editing: false,
    }));
    window.API_KEY_ROWS = API_KEY_ROWS;
    DELETED_KEY_ROWS = (data.deleted_api_keys || []).map(k => ({
      id: k.id || '',
      name: k.name || '',
      realm: k.realm || '',
      created_at: k.created_at || '',
      deleted_at: k.deleted_at || '',
    }));
    renderKeyRows();
    updateUI();
    const set = (id, value) => { const el = document.getElementById(id); if(el) el.textContent = value || '-'; };
    set('setVersion', data.version);
    set('setAccountsDir', data.accounts_dir);
    set('setUsageDir', data.usage_dir);
    set('setFile', data.settings_file);
    set('setSettingsFile', data.settings_file);
    // 更新状态来自 /updates 而不是 /settings，单独拉一次；它自带兜底，
    // 失败只写在自己的状态行里，不会把上面这些区块一起带走。
    loadUpdateStatus();
    // 备份份数走 /settings 的既有字段；备份列表与当前版本另有一份只读接口，
    // 拉不到时只影响「版本回退」卡片，不影响上面的更新卡片。
    const keepSelect = document.getElementById('setKeepBackups');
    if(keepSelect){
      keepSelect.value = String((typeof data.keep_backups === 'number') ? data.keep_backups : 1);
    }
    loadUpdateBackups();
      // 用量聚合保留窗口：没改过时后端给的就是内置默认（小时层 48 / 日桶 32），
      // 所以「等于默认」按默认态显示，只有被调过才标出实际值。
      const usageKeep = data.usage || {};
      const usageHourKeep = usageKeep.hour_keep_hours;
      const usageDayKeep = usageKeep.day_keep_days;
      const usageHourInput = document.getElementById('setUsageHourKeep');
      if(usageHourInput){
        usageHourInput.value = (typeof usageHourKeep === 'number') ? usageHourKeep : 48;
      }
      const usageDayInput = document.getElementById('setUsageDayKeep');
      if(usageDayInput){
        usageDayInput.value = (typeof usageDayKeep === 'number') ? usageDayKeep : 32;
      }
      const usageKeepState = document.getElementById('setUsageKeepState');
      if(usageKeepState){
        const hourDefault = (typeof usageHourKeep !== 'number') || usageHourKeep === 48;
        const dayDefault = (typeof usageDayKeep !== 'number') || usageDayKeep === 32;
        const isDefault = hourDefault && dayDefault;
        usageKeepState.textContent = isDefault
          ? '(默认 48 小时 / 32 天)'
          : '(' + (typeof usageHourKeep === 'number' ? usageHourKeep : 48) + ' 小时 / '
              + (typeof usageDayKeep === 'number' ? usageDayKeep : 32) + ' 天)';
        usageKeepState.style.color = isDefault ? 'var(--dim)' : 'var(--accent2)';
      }
    if(showToast) toast('设置已刷新', 'ok');
  } catch(e) {
    if(showToast) toast('读取设置失败: ' + e.message, 'bad');
  }
}

function applyUiLanguageSelect(value){
  const select = document.getElementById('setUiLanguage');
  if(select) select.value = value || 'zh';
  const state = document.getElementById('setUiLanguageState');
  if(state){
    const label = value === 'zh-Hant' ? '(正體中文)' : (value === 'en' ? '(English)' : '(简体中文)');
    state.textContent = label;
    state.style.color = 'var(--dim)';
  }
}

async function saveUiLanguage(btn){
  const select = document.getElementById('setUiLanguage');
  const value = select ? select.value : 'zh';
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {ui_language: value});
    toast('实例默认语言已保存', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

function clearUiLanguageOverride(btn){
  try { localStorage.removeItem('wb-lang'); } catch(e){}
  if(btn) btn.disabled = true;
  location.reload();
}

async function savePanelPassword(btn){
  const cur = (document.getElementById('setPwdCurrent') || {}).value || '';
  const nw = (document.getElementById('setPwdNew') || {}).value || '';
  if(!cur || !nw){ toast('请填写当前密码和新密码', 'warn'); return; }
  btn.disabled = true;
  try {
    const data = await postJSON('/panel/password', {current: cur, new: nw});
    if(data.token){
      PANEL_TOKEN = data.token;
      try { sessionStorage.setItem(PANEL_STORE, PANEL_TOKEN); } catch(e) {}
    }
    document.getElementById('setPwdCurrent').value = '';
    document.getElementById('setPwdNew').value = '';
    toast('面板密码已更新', 'ok');
    const pwdState = document.getElementById('setPwdState');
    // 以服务端返回为准：如果保存落到另一个实例，界面上不能假装已生效。
    const usingDefault = (data.using_default_password !== undefined)
      ? !!data.using_default_password : (nw === 'admin');
    if(pwdState){
      pwdState.textContent = usingDefault ? '(当前为默认密码)' : '(已自定义)';
      pwdState.style.color = usingDefault ? 'var(--warn)' : 'var(--accent2)';
    }
  } catch(e) {
    toast('修改失败: ' + e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
}

// ---- 限额（全局默认 + 可选分版本） -------------------------------------
// 五个护栏共用一张表：一列一个作用域。国际版/国内版留空即继承全局默认，
// 所以“不分版本”只是“两列都留空”的一个特例，不必再另存一套开关。
const LIMIT_FIELDS = [
  {key:'reserve_credits',         id:'Reserve',         label:'保留积分'},
  {key:'daily_token_limit',       id:'DailyToken',      label:'每日 Token 限额'},
  {key:'daily_credit_limit',      id:'DailyCredit',     label:'每日积分限额'},
  {key:'model_daily_token_limit', id:'ModelDailyToken', label:'按模型每日 Token 限额'},
  {key:'expiring_window_days',    id:'ExpiringWindow',  label:'临期积分优先'},
];
const LIMIT_SCOPES = [
  {scope:'global', prefix:'Global', label:'全局默认'},
  {scope:'intl',   prefix:'Intl',   label:'国际版'},
  {scope:'cn',     prefix:'Cn',     label:'国内版'},
];

function limitEl(prefix, id){
  return document.getElementById('limit' + prefix + id);
}

function limitHasOverride(entry){
  return LIMIT_SCOPES.slice(1).some(
    ({scope}) => entry[scope] !== null && entry[scope] !== undefined);
}

// 括号里写清这条护栏当前生效的值：先全局，再任何被单独设置的版本。
function limitBadge(entry){
  const bits = [];
  const globalValue = Number(entry.global || 0);
  if(globalValue > 0) bits.push('全局 ' + fmt(globalValue));
  for(const {scope, label} of LIMIT_SCOPES.slice(1)){
    const value = entry[scope];
    if(value !== null && value !== undefined) bits.push(label + ' ' + fmt(value));
  }
  return bits.length ? ('(' + bits.join(' · ') + ')') : '(全部关闭)';
}

function toggleLimitRealms(show){
  document.querySelectorAll('.limit-realm-col').forEach(el => {
    el.style.display = show ? '' : 'none';
  });
}

function applyLimits(data){
  const limits = data.limits || {};
  const anyOverride = LIMIT_FIELDS.some(
    field => limitHasOverride(limits[field.key] || {}));
  for(const field of LIMIT_FIELDS){
    const entry = limits[field.key] || {};
    const globalValue = Number(entry.global || 0);
    const globalInput = limitEl('Global', field.id);
    if(globalInput) globalInput.value = String(globalValue);
    for(const {scope, prefix} of LIMIT_SCOPES.slice(1)){
      const input = limitEl(prefix, field.id);
      const raw = entry[scope];
      const inherited = (raw === null || raw === undefined);
      if(input){
        input.value = inherited ? '' : String(raw);
        // 占位符直接写出继承到的那个数，省得再翻回全局列对照。
        input.placeholder = '继承 ' + fmt(globalValue);
      }
    }
    const state = document.getElementById('set' + field.id + 'State');
    if(state){
      state.textContent = limitBadge(entry);
      state.style.color = (globalValue > 0 || anyOverride) ? 'var(--accent2)' : 'var(--dim)';
    }
  }
  const expiringToggle = document.getElementById('setExpiringWindowToggle');
  if(expiringToggle){
    expiringToggle.checked =
      Number((limits.expiring_window_days || {}).global || 0) > 0;
  }
  const toggle = document.getElementById('setLimitsPerRealm');
  if(toggle) toggle.checked = anyOverride;
  toggleLimitRealms(anyOverride);
  const sectionState = document.getElementById('setLimitsState');
  if(sectionState){
    sectionState.textContent = anyOverride ? '(已分版本设置)' : '(全局生效)';
    sectionState.style.color = anyOverride ? 'var(--accent2)' : 'var(--dim)';
  }
}

function limitFormValue(prefix, field){
  const input = limitEl(prefix, field.id);
  return input ? String(input.value).trim() : '';
}

async function saveLimits(btn){
  const perRealmBox = document.getElementById('setLimitsPerRealm');
  const showRealms = !!(perRealmBox && perRealmBox.checked);
  const payload = {};
  for(const field of LIMIT_FIELDS){
    const globalValue = Math.floor(Number(limitFormValue('Global', field) || 0));
    if(!Number.isFinite(globalValue) || globalValue < 0){
      toast('「' + field.label + '」的全局默认请填 0 或正整数', 'warn'); return;
    }
    const entry = {global: globalValue};
    for(const {scope, prefix, label} of LIMIT_SCOPES.slice(1)){
      const raw = limitFormValue(prefix, field);
      // 收起分版本、或该版本留空，都表示“继承全局”；服务端收到 null 会清掉旧覆盖值。
      if(!showRealms || raw === ''){ entry[scope] = null; continue; }
      const value = Math.floor(Number(raw));
      if(!Number.isFinite(value) || value < 0){
        toast('「' + field.label + '」的' + label + '请填 0 或正整数（留空 = 继承全局）', 'warn'); return;
      }
      entry[scope] = value;
    }
    payload[field.key] = entry;
  }
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {limits: payload});
    toast('限额已保存', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

/* 临期积分优先的开关：开着等价于「窗口天数 > 0」，关掉就写 0。窗口天数本身
   仍可在表格里单独填（例如 3 天、14 天），开关只负责一键开/关。 */
async function toggleExpiringWindow(el){
  const enabled = !!(el && el.checked);
  const input = document.getElementById('limitGlobalExpiringWindow');
  const current = Math.floor(Number(input && input.value) || 0);
  const value = enabled ? (current > 0 ? current : 7) : 0;
  if(input) input.value = String(value);
  el.disabled = true;
  try {
    await postJSON('/settings/save', {limits:{expiring_window_days:{global:value}}});
    toast(enabled ? '已开启临期积分优先' : '已关闭临期积分优先', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
    el.checked = !enabled;
  } finally {
    el.disabled = false;
  }
}

/* 积分数据的后台刷新间隔。分派读的是账号上缓存的积分余额，只有签到/每日活跃
   会刷新它；这里给一个 TTL，超时的账号由后台每十分钟挑最旧的一个刷一次，填 0
   关闭后台刷新。 */
async function saveCreditsRefreshHours(btn){
  const input = document.getElementById('setCreditsRefreshHours');
  const value = Number(input && input.value);
  if(!Number.isFinite(value) || value < 0){
    toast('积分数据刷新间隔请填 0 或正整数（0 = 关闭后台刷新）', 'warn'); return;
  }
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {credits_refresh_hours: value});
    toast('已保存积分数据刷新间隔', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

/* 价估算总开关。关闭时整块功能从界面上撤掉（价格列、KPI 卡片、取价子控件、
   未定价清单），只留开关本身；后端同时停止取价与算价。历史数据不动。 */
function applyPricingEnabled(on){
  const enabled = on !== false;
  PRICING_ON = enabled;
  document.body.classList.toggle('pricing-off', !enabled);
  const state = document.getElementById('setPricingEnabledState');
  if(state){
    state.textContent = enabled ? '(已启用)' : '(已关闭)';
    state.style.color = enabled ? 'var(--accent2)' : 'var(--dim)';
  }
  const note = document.getElementById('pricingOffNote');
  if(note) note.style.display = enabled ? 'none' : '';
  // 关闭时除开关本身外，取价控件、当前生效策略与未定价清单一起隐藏。
  const details = document.getElementById('pricingDetails');
  if(details) details.style.display = enabled ? '' : 'none';
  const controls = document.getElementById('pricingControls');
  if(controls) controls.style.display = enabled ? 'flex' : 'none';
}

async function savePricingEnabled(btn){
  const el = document.getElementById('setPricingEnabled');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {pricing_enabled: value});
    toast(value ? '已启用 OpenRouter 价估算' : '已关闭 OpenRouter 价估算', 'ok');
    loadSettings();
    if(value) loadPricingStatus();
  } catch(e) {
    // 开关点一下即生效，保存失败就把它拨回原位，别让界面和后端各说各话。
    if(el) el.checked = !value;
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function savePricingMinutes(btn){
  const el = document.getElementById('setPricingMinutes');
  const value = Number((el || {}).value || 0);
  if(!Number.isFinite(value) || value < 0){ toast('请填写 0 或正数（分钟）', 'warn'); return; }
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {pricing_refresh_minutes: value});
    toast(value > 0 ? ('已启用：每 ' + value + ' 分钟取一次价') : '已关闭自动取价（仍可手动取一次）', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function loadPricingStatus(){
  const meta = document.getElementById('setPricingMeta');
  const box = document.getElementById('setPricingCurrent');
  if(!meta) return;
  try {
    const st = await getJSON('/pricing');
    const bits = [];
    if(st.current_at_label) bits.push('当前生效 ' + st.current_at_label + ' 那一份（' + (st.models || 0) + ' 个模型）');
    else bits.push('还没取过价，暂用出厂快照');
    if(st.last_run_label) bits.push('上次取价 ' + st.last_run_label);
    if(st.enabled && st.next_run_label) bits.push('下次 ' + st.next_run_label);
    bits.push('策略表 ' + (st.policies || 0) + ' 条');
    if(st.last_error) bits.push('上次失败：' + st.last_error);
    meta.textContent = '　' + bits.join('；');
    meta.style.color = st.last_error ? 'var(--warn)' : 'var(--dim)';
    if(box){
      const cur = st.current || {};
      const models = Object.keys(cur);
      box.innerHTML = models.length
        ? '<details><summary style="cursor:pointer">展开查看每个模型当前生效的策略</summary>'
          + '<div style="margin-top:6px;max-height:260px;overflow:auto;font-family:monospace;line-height:1.7">'
          + models.map(m => esc(m) + ' → ' + esc(cur[m].id || '')
              + (cur[m].via === 'variant' ? '（继承自 ' + esc(cur[m].inherited_from || '?') + '）' : '')
              + (cur[m].label ? '　' + esc(cur[m].label) + ' 首次取到' : '')).join('<br>')
          + '</div></details>'
        : '';
    }
    renderPricingGaps(st);
  } catch(e) {
    meta.textContent = '';
    if(box) box.innerHTML = '';
  }
}

/* ---- unpriced models: what shows a dash, why, and a way to map it ---- */
const PRICING_GAP_LABELS = {
  or_missing: {text: 'OpenRouter 无对应', color: 'var(--warn)'},
  variant_unmatched: {text: '变体名未命中基准', color: 'var(--warn)'},
  alias: {text: '虚拟别名（非实体模型）', color: 'var(--dim)'},
};

function safeModelId(value){
  // Model ids and OpenRouter ids are [A-Za-z0-9._/:-]; anything else is
  // dropped before it reaches an attribute or an inline handler argument.
  return String(value == null ? '' : value).replace(/[^A-Za-z0-9._/:\-]/g, '');
}

function renderPricingGaps(st){
  const box = document.getElementById('setPricingGaps');
  if(!box) return;
  const gaps = Array.isArray(st.gaps) ? st.gaps : [];
  const sum = st.gap_summary || {};
  const head = '<div style="font-size:12px;color:var(--dim);margin-top:10px">'
    + '<b>未定价模型</b>：' + fmt(sum.total || 0) + ' 个需要人工关注'
    + (sum.or_missing ? '（无对应 ' + fmt(sum.or_missing) + '）' : '')
    + (sum.variant_unmatched ? '（变体未命中 ' + fmt(sum.variant_unmatched) + '）' : '')
    + (sum.aliases ? '；另有 ' + fmt(sum.aliases) + ' 个虚拟别名不参与统计' : '')
    + (sum.variants_enabled === false ? '；变体继承当前已关闭' : '')
    + '</div>';
  if(!gaps.length){
    box.innerHTML = head + '<div style="font-size:12px;color:var(--accent2);margin-top:4px">全部模型都有价。</div>';
    return;
  }
  const rows = gaps.map(g => {
    const model = safeModelId(g.model);
    const label = PRICING_GAP_LABELS[g.reason] || {text: g.reason || '未定价', color: 'var(--dim)'};
    const cands = (g.candidates || []).map(c =>
      '<a href="javascript:void(0)" data-or-id="' + safeModelId(c)
      + '" onclick="fillPricingMapping(this)" style="color:var(--accent2)">' + esc(c) + '</a>'
    ).join('、');
    const editable = g.reason !== 'alias';
    const control = editable
      ? '<input placeholder="vendor/model" style="width:190px;background:var(--panel2);'
        + 'border:1px solid var(--line);border-radius:6px;padding:4px 8px;color:var(--fg);font:inherit;font-size:12px">'
        + '<button onclick="savePricingMapping(this)" style="font-size:12px;padding:4px 10px">登记</button>'
      : '<span style="color:var(--dim);font-size:12px">无需登记</span>';
    return '<div data-model="' + model + '" style="display:flex;align-items:center;gap:8px;'
      + 'flex-wrap:wrap;padding:5px 0;border-top:1px solid var(--line)">'
      + '<span class="mono" style="font-size:12px;min-width:180px">' + esc(g.model) + '</span>'
      + '<span style="font-size:12px;color:' + label.color + '">' + esc(label.text) + '</span>'
      + '<span style="font-size:12px;color:var(--dim)">' + (cands ? '候选：' + cands : '') + '</span>'
      + control + '</div>';
  }).join('');
  box.innerHTML = head
    + '<details style="margin-top:4px"><summary style="cursor:pointer;font-size:12px">展开逐条查看 / 手填 OpenRouter id</summary>'
    + '<div style="margin-top:4px;max-height:320px;overflow:auto">' + rows + '</div>'
    + '<div style="font-size:11px;color:var(--dim);margin-top:6px">手填的映射存在数据目录的 pricing-overrides.json（不改源码），登记后立即触发一次取价；留空提交即删除该映射。候选只是字面相似度建议，不自动采用。</div>'
    + '</details>';
}

function fillPricingMapping(link){
  const row = link.closest('[data-model]');
  const input = row && row.querySelector('input');
  if(input) input.value = link.getAttribute('data-or-id') || '';
}

async function savePricingMapping(btn){
  const row = btn.closest('[data-model]');
  if(!row) return;
  const model = row.getAttribute('data-model');
  const input = row.querySelector('input');
  const orId = (input && input.value || '').trim();
  btn.disabled = true;
  try {
    const res = await postJSON('/pricing/mapping', {model: model, or_id: orId});
    toast(res.msg || '已记录', res.known || !orId ? 'ok' : 'warn');
    setTimeout(loadPricingStatus, 3000);
    setTimeout(loadPricingStatus, 12000);
  } catch(e) {
    toast('登记失败: ' + e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
}

async function savePricingVariant(btn){
  const el = document.getElementById('setPricingVariant');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    const res = await postJSON('/settings/save', {pricing_variant_inherit: value});
    toast(value
      ? '已开启：带渠道后缀的模型按基准模型继承定价（仅在唯一命中时）'
      : '已关闭变体继承：带后缀的名字不再自动取价', 'ok');
    if(res.pricing_refresh_started) setTimeout(loadPricingStatus, 4000);
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function refreshPricingNow(btn){
  if(btn) btn.disabled = true;
  try {
    const st = await postJSON('/pricing/refresh', {});
    toast(st.msg || '已开始抓取', 'ok');
    // The fetch runs on the server's own thread, so come back for the result
    // instead of holding the button.
    setTimeout(loadPricingStatus, 4000);
    setTimeout(loadPricingStatus, 15000);
  } catch(e) {
    toast('触发失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveAnthropicMessages(btn){
  const el = document.getElementById('setAnthropicMessages');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {enable_anthropic_messages: value});
    toast(value ? '已启用：Anthropic Messages 接口' : '已关闭：Anthropic Messages 接口', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveReasoningEffortMap(btn){
  const el = document.getElementById('setReasoningEffortMap');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {reasoning_effort_map: value});
    toast(value ? '已启用：思考等级挡位映射' : '已关闭：思考等级原样透传', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveModelsOnlyAvailable(btn){
  const el = document.getElementById('setModelsOnlyAvailable');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {models_only_available: value});
    toast(value ? '已启用：/v1/models 只列出可用模型' : '已关闭：/v1/models 返回全量目录', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveFilterModelsByKey(btn){
  const el = document.getElementById('setFilterModelsByKey');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {filter_models_by_key: value});
    toast(value ? '已启用：按 API Key 过滤 /v1/models' : '已关闭：/v1/models 返回全部模型', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveCreditFallback(btn){
  const el = document.getElementById('setCreditFallback');
  const value = !!(el && el.checked);
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {free_model_credit_fallback: value});
    toast(value ? '已启用：免费额度耗尽自动回退付费积分版' : '已关闭自动回退付费积分版', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function saveAutoSwitchProduct(btn){
  const el = document.getElementById('setAutoSwitch');
  const value = !!(el && el.checked);
  const products = [];
  [['setAutoSwitchWB', 'workbuddy'], ['setAutoSwitchVSC', 'vscode'],
   ['setAutoSwitchCLI', 'cli']].forEach(function(pair){
    const input = document.getElementById(pair[0]);
    if(input && input.checked) products.push(pair[1]);
  });
  if(!products.length){
    toast('至少选择一个身份', 'bad');
    return;
  }
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {auto_switch_product: value,
                                      auto_switch_products: products});
    toast(value ? '已启用：429 时自动切换出站身份' : '已关闭自动切换出站身份', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

let IDENTITY_VERSION_DEFAULTS = {};
let IDENTITY_VERSION_CURRENT = {};
const IDENTITY_VERSION_FIELDS = [
  ['desktop_intl', 'WorkBuddy 国际版', 'setIdentityVerDesktopIntl'],
  ['desktop_cn', 'WorkBuddy 国内版', 'setIdentityVerDesktopCn'],
  ['desktop_cli', '桌面端内置 CLI', 'setIdentityVerDesktopCli'],
  ['vscode_product', 'VSCode 扩展', 'setIdentityVerVscodeProduct'],
  ['vscode_ide', '宿主 VSCode', 'setIdentityVerVscodeIde'],
  ['cli', 'CodeBuddy CLI', 'setIdentityVerCli'],
  ['sdk', 'OpenAI Node SDK', 'setIdentityVerSdk'],
  ['node', 'Node 版本', 'setIdentityVerNode'],
];
function renderIdentityVersions(){
  const summary = document.getElementById('identityVersionSummary');
  if(summary){
    summary.innerHTML = IDENTITY_VERSION_FIELDS.map(function(item){
      const key = item[0], label = item[1];
      const fallback = IDENTITY_VERSION_DEFAULTS[key] || '-';
      const current = IDENTITY_VERSION_CURRENT[key] || fallback;
      const changed = IDENTITY_VERSION_CURRENT[key]
        && IDENTITY_VERSION_CURRENT[key] !== IDENTITY_VERSION_DEFAULTS[key];
      return '<div>' + esc(label) + '：<span class="mono" style="color:var(--fg)">'
        + esc(current) + '</span> '
        + (changed
            ? '<span class="badge warn" style="font-size:10px">已修改（默认 ' + esc(fallback) + '）</span>'
            : '<span style="color:var(--dim-light)">默认</span>')
        + '</div>';
    }).join('');
  }
  IDENTITY_VERSION_FIELDS.forEach(function(item){
    const el = document.getElementById(item[2]);
    if(el) el.value = IDENTITY_VERSION_CURRENT[item[0]] || '';
  });
}
function toggleIdentityVersionEditor(){
  const el = document.getElementById('identityVersionEditor');
  if(el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
async function saveIdentityVersions(btn){
  const passwordEl = document.getElementById('setIdentityVersionPassword');
  const password = String((passwordEl && passwordEl.value) || '');
  if(!password){ toast('请输入面板密码', 'warn'); return; }
  const versions = {};
  IDENTITY_VERSION_FIELDS.forEach(function(item){
    const el = document.getElementById(item[2]);
    const value = String((el && el.value) || '').trim();
    if(value) versions[item[0]] = value;
  });
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {identity_versions: versions, panel_password: password});
    toast('出站身份版本号已保存', 'ok');
    if(passwordEl) passwordEl.value = '';
    const editor = document.getElementById('identityVersionEditor');
    if(editor) editor.style.display = 'none';
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function savePrivateData(btn){
  const el = document.getElementById('setPrivateData');
  const value = el ? el.value : 'auto';
  if(['auto', 'true', 'false'].indexOf(value) < 0) return;
  if(btn) btn.disabled = true;
  try {
    await postJSON('/settings/save', {identity: {private_data: value}});
    toast('已保存数据用途声明: ' + value, 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}

/* 随机提示词开关。后端只接受严格布尔，所以这里直接发勾选状态；存失败时把开关
   拨回去，免得界面显示一个其实没落盘的状态。 */
async function saveDailyChatRandomPrompt(){
  const el = document.getElementById('setDailyChatRandomPrompt');
  const value = !!(el && el.checked);
  try {
    await postJSON('/settings/save', {daily_chat_web_random_prompt: value});
    toast(value ? '已启用：网页活跃随机提示词'
                : '已关闭：网页活跃固定发 Hi', 'ok');
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
    if(el) el.checked = !value;
  }
}

let PROMPT_PICKER_BUILTIN = [];
let PROMPT_PICKER_SELECTED = new Set();
let PROMPT_PICKER_CUSTOM = new Set();

function promptPickerItems(){
  const out = [];
  const seen = new Set();
  PROMPT_PICKER_BUILTIN.concat(Array.from(PROMPT_PICKER_CUSTOM)).forEach(function(p){
    if(p && !seen.has(p)){ seen.add(p); out.push(p); }
  });
  return out;
}
function updatePromptPickerState(){
  const el = document.getElementById('setDailyChatPromptsState');
  if(!el) return;
  el.textContent = PROMPT_PICKER_SELECTED.size
    ? ('已选 ' + PROMPT_PICKER_SELECTED.size + ' 条')
    : '默认全部内置 300 条';
}
function renderPromptPicker(){
  const list = document.getElementById('promptPickerList');
  if(!list) return;
  const items = promptPickerItems();
  list.innerHTML = items.map(function(p){
    const checked = PROMPT_PICKER_SELECTED.has(p) ? ' checked' : '';
    return '<label class="prompt-pick-row" data-search="' + esc(p.toLowerCase()) + '" style="display:flex;align-items:flex-start;gap:8px;padding:4px 2px;font-size:13px;cursor:pointer"><input type="checkbox" value="' + esc(p) + '"' + checked + ' onchange="togglePromptPicker(this)"><span>' + esc(p) + '</span></label>';
  }).join('') || '<div style="font-size:12px;color:var(--dim);padding:4px">没有提示词</div>';
  updatePromptPickerState();
}
function openPromptPicker(){
  const stored = Array.isArray(window.DAILY_CHAT_PROMPTS_STORED)
    ? window.DAILY_CHAT_PROMPTS_STORED : [];
  PROMPT_PICKER_CUSTOM = new Set(stored.filter(function(p){
    return PROMPT_PICKER_BUILTIN.indexOf(p) < 0;
  }));
  PROMPT_PICKER_SELECTED = new Set(stored.length ? stored : PROMPT_PICKER_BUILTIN);
  renderPromptPicker();
  const mask = document.getElementById('promptPickerMask');
  if(mask) mask.style.display = 'flex';
  const search = document.getElementById('promptPickerSearch');
  if(search) search.value = '';
}
function closePromptPicker(){
  const mask = document.getElementById('promptPickerMask');
  if(mask) mask.style.display = 'none';
}
function togglePromptPicker(el){
  if(el.checked) PROMPT_PICKER_SELECTED.add(el.value);
  else PROMPT_PICKER_SELECTED.delete(el.value);
  updatePromptPickerState();
}
function setPromptPickerAll(on){
  const list = document.getElementById('promptPickerList');
  if(!list) return;
  list.querySelectorAll('input[type=checkbox]').forEach(function(el){
    el.checked = on;
    if(on) PROMPT_PICKER_SELECTED.add(el.value);
    else PROMPT_PICKER_SELECTED.delete(el.value);
  });
  updatePromptPickerState();
}
function filterPromptPicker(){
  const input = document.getElementById('promptPickerSearch');
  const list = document.getElementById('promptPickerList');
  if(!list) return;
  const q = String((input && input.value) || '').trim().toLowerCase();
  list.querySelectorAll('.prompt-pick-row').forEach(function(el){
    el.style.display = !q || (el.getAttribute('data-search') || '').indexOf(q) >= 0 ? 'flex' : 'none';
  });
}
function addPromptPickerCustom(){
  const ta = document.getElementById('promptPickerCustom');
  const lines = String((ta && ta.value) || '').split('\n')
    .map(function(s){ return s.trim(); }).filter(function(s){ return s.length > 0; });
  lines.forEach(function(p){
    PROMPT_PICKER_CUSTOM.add(p);
    PROMPT_PICKER_SELECTED.add(p);
  });
  if(ta) ta.value = '';
  renderPromptPicker();
}
async function savePromptPicker(btn){
  const prompts = Array.from(PROMPT_PICKER_SELECTED);
  if(btn) btn.disabled = true;
  try {
    const res = await postJSON('/settings/save', {daily_chat_web_prompts: prompts});
    const stored = Array.isArray(res && res.daily_chat_web_prompts)
      ? res.daily_chat_web_prompts : prompts;
    window.DAILY_CHAT_PROMPTS_STORED = stored;
    toast(stored.length ? ('已保存 ' + stored.length + ' 条候选提示词')
                        : '已清空：使用全部内置 300 条', 'ok');
    closePromptPicker();
    loadSettings();
  } catch(e) {
    toast('保存失败: ' + e.message, 'bad');
  } finally {
    if(btn) btn.disabled = false;
  }
}
