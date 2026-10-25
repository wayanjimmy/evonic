/* Tools pane: list/filter/paginate + tool editor modal (info, backend code,
 * mock, live test). Extracted from settings.html — logic unchanged. */

window.settingsTools = {
    PAGE_SIZE: 10,
    tools: [],
    page: 1,
    query: "",
    whenLoaded: null,
    _resolveLoaded: null,

    init() {
        if (!this.whenLoaded) {
            this.whenLoaded = new Promise((resolve) => {
                this._resolveLoaded = resolve;
            });
        }
        this.load();
    },

    async load() {
        try {
            const data = await apiGet("/api/settings/tools");
            this.tools = data.tools || [];
            this.page = 1;
            this.render();
            if (this._resolveLoaded) {
                this._resolveLoaded();
                this._resolveLoaded = null;
            }
        } catch (error) {
            console.error("Error loading tools:", error);
        }
    },

    filter() {
        this.query = document.getElementById("tools-filter").value.toLowerCase();
        this.page = 1;
        this.render();
    },

    goPage(page) {
        const filtered = this._filtered();
        const totalPages = Math.max(1, Math.ceil(filtered.length / this.PAGE_SIZE));
        this.page = Math.max(1, Math.min(page, totalPages));
        this.render();
    },

    _filtered() {
        if (!this.query) return this.tools;
        const q = this.query;
        return this.tools.filter(
            (t) =>
                (t.name || "").toLowerCase().includes(q) ||
                (t.description || "").toLowerCase().includes(q) ||
                (t.function && (t.function.name || "").toLowerCase().includes(q)) ||
                (t.id || "").toLowerCase().includes(q),
        );
    },

    render() {
        const container = document.getElementById("tools-list");
        const filtered = this._filtered();
        const totalPages = Math.max(1, Math.ceil(filtered.length / this.PAGE_SIZE));
        if (this.page > totalPages) this.page = totalPages;

        const start = (this.page - 1) * this.PAGE_SIZE;
        const pageItems = filtered.slice(start, start + this.PAGE_SIZE);

        const countEl = document.getElementById("tools-count");
        if (countEl) {
            countEl.textContent = this.query
                ? `${filtered.length} of ${this.tools.length} tools`
                : `${this.tools.length} tool${this.tools.length !== 1 ? "s" : ""}`;
        }

        const paginationEl = document.getElementById("tools-pagination");
        const pageInfoEl = document.getElementById("tools-page-info");
        const prevBtn = document.getElementById("tools-prev");
        const nextBtn = document.getElementById("tools-next");
        if (paginationEl) {
            if (totalPages > 1) {
                paginationEl.classList.remove("hidden");
                pageInfoEl.textContent = `Page ${this.page} of ${totalPages}`;
                prevBtn.disabled = this.page <= 1;
                nextBtn.disabled = this.page >= totalPages;
            } else {
                paginationEl.classList.add("hidden");
            }
        }

        if (this.tools.length === 0) {
            container.innerHTML =
                '<div class="st-empty">No tools found.</div>';
            return;
        }
        if (filtered.length === 0) {
            container.innerHTML = `<div class="st-empty">No tools match "<strong>${this.query}</strong>".</div>`;
            return;
        }

        container.innerHTML = pageItems
            .map(
                (tool) => `
        <div class="st-card" tabindex="0" role="button" onclick="settingsTools.openEditor('${tool.id}')"
             onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();settingsTools.openEditor('${tool.id}')}">
            <div class="st-card-head">
                <h4 class="st-card-name">${tool.name}</h4>
                <span class="st-chip ${tool.mock_response_type === "javascript" ? "is-js" : "is-json"}">${tool.mock_response_type === "javascript" ? "JS" : "JSON"}</span>
                ${tool.no_mock ? '<span class="st-chip is-real">real backend</span>' : ""}
                <button class="st-card-del" title="Delete tool" aria-label="Delete tool" onclick="event.stopPropagation(); settingsTools.remove('${tool.id}')"><i data-lucide="trash-2"></i></button>
            </div>
            <p class="st-card-desc">${tool.description || "No description"}</p>
            <code class="st-card-code">${tool.function ? tool.function.name : tool.id}</code>
        </div>
    `,
            )
            .join("");
        if (window.refreshLucideIcons) refreshLucideIcons();
    },

    async openEditor(toolId) {
        try {
            const tool = await apiGet(`/api/settings/tools/${encodeURIComponent(toolId)}`);

            $("#tool-modal-title").text("Edit Tool");
            $("#tool-edit-id").val(tool.id);
            $("#tool-id-input").val(tool.id);
            $("#tool-name").val(tool.name || "");
            $("#tool-description").val(tool.description || "");
            $("#tool-func-name").val(tool.function ? tool.function.name : "");
            $("#tool-func-description").val(
                tool.function ? tool.function.description || "" : "",
            );
            $("#tool-parameters").val(
                tool.function && tool.function.parameters
                    ? JSON.stringify(tool.function.parameters, null, 2)
                    : "",
            );
            $("#tool-mock-type").val(tool.mock_response_type || "json");
            document.getElementById("tool-no-mock").checked = !!tool.no_mock;

            // Mock response
            if (tool.mock_response_type === "javascript") {
                $("#tool-mock-response").val(tool.mock_response || "");
            } else {
                $("#tool-mock-response").val(
                    typeof tool.mock_response === "object"
                        ? JSON.stringify(tool.mock_response, null, 2)
                        : tool.mock_response || "",
                );
            }

            // Pre-fill test args from parameter schema
            let sampleArgs = "{}";
            const params =
                tool.function &&
                tool.function.parameters &&
                tool.function.parameters.properties;
            if (params) {
                const sample = {};
                for (const [k, v] of Object.entries(params)) {
                    sample[k] =
                        v.type === "number" || v.type === "integer"
                            ? 0
                            : v.type === "boolean"
                              ? false
                              : "";
                }
                sampleArgs = JSON.stringify(sample, null, 2);
            }
            $("#tool-test-args-real").val(sampleArgs);
            $("#tool-test-args-mock").val(sampleArgs);
            $("#tool-test-result-real, #tool-test-result-mock").addClass("hidden");
            $("#tool-test-status-real, #tool-test-status-mock").addClass("hidden");
            $("#tool-backend-save-status").addClass("hidden");

            // Fetch backend Python code
            const fnName = (tool.function && tool.function.name) || tool.id;
            try {
                const backend = await apiGet(
                    `/api/settings/tools/${encodeURIComponent(fnName)}/backend`,
                );
                if (backend.exists) {
                    $("#tool-backend-code").val(backend.code);
                } else {
                    $("#tool-backend-code").val(
                        `"""Backend implementation for the ${fnName} tool."""\n\n\ndef execute(agent, args: dict) -> dict:\n    # agent: dict with agent_id, agent_name, user_id, channel_id, session_id\n    # args: dict of arguments passed by the LLM\n    return {"result": "not implemented"}\n`,
                    );
                }
            } catch (e) {
                $("#tool-backend-code").val(
                    `"""Backend implementation for the ${fnName} tool."""\n\n\ndef execute(agent, args: dict) -> dict:\n    return {"result": "not implemented"}\n`,
                );
            }

            this.toggleMockResponseHelp();
            this.switchModalTab("info");
            this.switchModalOpTab("prod");
            openModal("tool-modal");
        } catch (error) {
            console.error("Error loading tool:", error);
            if (window.toast) toast.show("Error loading tool: " + error, "error");
        }
    },

    async save() {
        const toolId = document.getElementById("tool-edit-id").value || null;

        let parameters = null;
        const paramsText = document.getElementById("tool-parameters").value.trim();
        if (paramsText) {
            try {
                parameters = JSON.parse(paramsText);
            } catch (e) {
                if (window.toast) toast.show("Invalid JSON in Parameters field", "error");
                return;
            }
        }

        const mockType = document.getElementById("tool-mock-type").value;
        let mockResponse = document.getElementById("tool-mock-response").value.trim();

        if (mockType === "json" && mockResponse) {
            try {
                mockResponse = JSON.parse(mockResponse);
            } catch (e) {
                if (window.toast) toast.show("Invalid JSON in Mock Response field", "error");
                return;
            }
        }

        const funcDef = {
            name: document.getElementById("tool-func-name").value,
            description: document.getElementById("tool-func-description").value || "",
            parameters: parameters || {
                type: "object",
                properties: {},
                required: [],
            },
        };

        const data = {
            id: document.getElementById("tool-id-input").value || undefined,
            name: document.getElementById("tool-name").value,
            description: document.getElementById("tool-description").value,
            function: funcDef,
            mock_response: mockResponse || null,
            mock_response_type: mockType,
            no_mock: document.getElementById("tool-no-mock").checked,
        };

        try {
            let result;
            if (toolId) {
                result = await apiPut(`/api/settings/tools/${toolId}`, data);
            } else {
                result = await apiPost("/api/settings/tools", data);
            }

            if (result.success) {
                closeModal("tool-modal");
                this.load();
                if (window.toast) toast.show("Tool saved", "success");
            } else {
                if (window.toast) toast.show("Error: " + result.error, "error");
            }
        } catch (error) {
            if (window.toast) toast.show("Error saving tool: " + error, "error");
        }
    },

    async remove(toolId) {
        if (
            !(await showConfirm({
                title: "Delete Tool",
                message: "Delete this tool? This cannot be undone.",
                confirmText: "Delete",
            }))
        )
            return;

        try {
            const result = await apiDelete(`/api/settings/tools/${toolId}`);
            if (result.success) {
                this.load();
            }
        } catch (error) {
            if (window.toast) toast.show("Error deleting tool: " + error, "error");
        }
    },

    toggleMockResponseHelp() {
        const type = $("#tool-mock-type").val();
        if (type === "javascript") {
            $("#tool-mock-help-json").addClass("hidden");
            $("#tool-mock-help-js").removeClass("hidden");
            $("#tool-mock-response").attr(
                "placeholder",
                'const args = JSON.parse(ARGS);\n// process args\nconsole.log(JSON.stringify({result: "value"}));',
            );
        } else {
            $("#tool-mock-help-json").removeClass("hidden");
            $("#tool-mock-help-js").addClass("hidden");
            $("#tool-mock-response").attr("placeholder", '{"result": "sample data"}');
        }
    },

    switchModalTab(tab) {
        const tabs = ["info", "operation"];
        tabs.forEach((t) => {
            $(`#tool-main-tab-${t}`).toggleClass("hidden", t !== tab);
            const $btn = $(`#tool-main-tab-btn-${t}`);
            $btn.toggleClass("border-indigo-500 text-indigo-600", t === tab);
            $btn.toggleClass("border-transparent text-gray-500", t !== tab);
        });
    },

    switchModalOpTab(tab) {
        const tabs = ["prod", "mock"];
        tabs.forEach((t) => {
            $(`#tool-op-tab-${t}`).toggleClass("hidden", t !== tab);
            const $btn = $(`#tool-op-tab-btn-${t}`);
            $btn.toggleClass("border-indigo-500 text-indigo-600", t === tab);
            $btn.toggleClass("border-transparent text-gray-500", t !== tab);
        });
    },

    async saveBackendCode() {
        const toolId = $("#tool-edit-id").val();
        if (!toolId) return;
        const code = $("#tool-backend-code").val();
        try {
            const res = await fetch(
                `/api/settings/tools/${encodeURIComponent(toolId)}/backend`,
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ code }),
                },
            );
            const data = await res.json();
            const $st = $("#tool-backend-save-status");
            if (data.success) {
                $st.text("Saved!")
                    .removeClass("hidden text-red-600")
                    .addClass("text-green-600");
            } else {
                $st.text(data.error || "Save failed")
                    .removeClass("hidden text-green-600")
                    .addClass("text-red-600");
            }
            setTimeout(() => $st.addClass("hidden"), 2000);
        } catch (e) {
            const $st = $("#tool-backend-save-status");
            $st.text("Network error")
                .removeClass("hidden text-green-600")
                .addClass("text-red-600");
            setTimeout(() => $st.addClass("hidden"), 2000);
        }
    },

    async testExec(mode) {
        const toolId = $("#tool-edit-id").val();
        if (!toolId) return;

        const $argsEl = $(`#tool-test-args-${mode}`);
        const $resultEl = $(`#tool-test-result-${mode}`);
        const $statusEl = $(`#tool-test-status-${mode}`);

        let args = {};
        const raw = $argsEl.val().trim();
        if (raw) {
            try {
                args = JSON.parse(raw);
            } catch (e) {
                $resultEl
                    .text("Invalid JSON: " + e.message)
                    .removeClass("hidden text-gray-700 border-gray-200")
                    .addClass("text-red-600 border-red-200")
                    .css("max-height", "200px");
                return;
            }
        }

        $statusEl.text("Running...").removeClass("hidden");
        $resultEl.addClass("hidden");

        // Mock mode: run client-side
        if (mode === "mock") {
            const tool = this.tools.find((t) => t.id === toolId);
            $statusEl.addClass("hidden");
            if (!tool || tool.mock_response == null) {
                $resultEl
                    .text("Error: No mock response defined for this tool")
                    .attr(
                        "class",
                        "mt-2 bg-red-50 border border-red-200 rounded-md p-3 text-xs font-mono whitespace-pre-wrap overflow-x-auto text-red-700",
                    )
                    .removeClass("hidden")
                    .css("max-height", "200px");
                return;
            }
            try {
                let result;
                if (tool.mock_response_type === "javascript") {
                    const lines = [];
                    const mockFn = new Function("ARGS", "console", tool.mock_response);
                    const fakeConsole = {
                        log: (...a) => lines.push(a.map(String).join(" ")),
                    };
                    const retVal = mockFn(args, fakeConsole);
                    if (lines.length > 0) {
                        result = lines.join("\n");
                    } else if (retVal !== undefined) {
                        result = retVal;
                    } else {
                        result = "(no output)";
                    }
                } else {
                    result = tool.mock_response;
                }
                $resultEl
                    .text(
                        typeof result === "string"
                            ? result
                            : JSON.stringify(result, null, 2),
                    )
                    .attr(
                        "class",
                        "mt-2 bg-gray-50 border border-gray-200 rounded-md p-3 text-xs font-mono whitespace-pre-wrap overflow-x-auto text-gray-700",
                    );
            } catch (e) {
                $resultEl
                    .text("Error: " + e.message)
                    .attr(
                        "class",
                        "mt-2 bg-red-50 border border-red-200 rounded-md p-3 text-xs font-mono whitespace-pre-wrap overflow-x-auto text-red-700",
                    );
            }
            $resultEl.removeClass("hidden").css("max-height", "200px");
            return;
        }

        // Real mode: server-side
        try {
            const res = await fetch(
                `/api/settings/tools/${encodeURIComponent(toolId)}/test`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ args, mode }),
                },
            );
            const data = await res.json();
            $statusEl.addClass("hidden");
            if (data.error) {
                $resultEl
                    .text("Error: " + data.error)
                    .attr(
                        "class",
                        "mt-2 bg-red-50 border border-red-200 rounded-md p-3 text-xs font-mono whitespace-pre-wrap overflow-x-auto text-red-700",
                    );
            } else {
                $resultEl
                    .text(JSON.stringify(data.result, null, 2))
                    .attr(
                        "class",
                        "mt-2 bg-gray-50 border border-gray-200 rounded-md p-3 text-xs font-mono whitespace-pre-wrap overflow-x-auto text-gray-700",
                    );
            }
            $resultEl.removeClass("hidden").css("max-height", "200px");
        } catch (e) {
            $statusEl.addClass("hidden");
            $resultEl
                .text("Network error: " + e.message)
                .attr(
                    "class",
                    "mt-2 bg-red-50 border border-red-200 rounded-md p-3 text-xs font-mono whitespace-pre-wrap overflow-x-auto text-red-700",
                )
                .removeClass("hidden")
                .css("max-height", "200px");
        }
    },
};
