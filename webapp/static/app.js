/* UplandScope — Client-side JavaScript */

(function() {
    const input = document.getElementById("neighborhood");
    const cityInput = document.getElementById("city_hint");
    const listEl = document.getElementById("autocomplete-list");
    const form = document.getElementById("mapForm");

    if (!input || !listEl) return;

    let debounceTimer = null;
    let selectedIndex = -1;
    let items = [];

    input.addEventListener("input", function() {
        clearTimeout(debounceTimer);
        const val = this.value.trim();
        if (val.length < 2) {
            closeList();
            return;
        }
        debounceTimer = setTimeout(() => fetchSuggestions(val), 250);
    });

    input.addEventListener("keydown", function(e) {
        if (e.key === "ArrowDown") {
            e.preventDefault();
            selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
            highlightItem();
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            selectedIndex = Math.max(selectedIndex - 1, -1);
            highlightItem();
        } else if (e.key === "Enter" && selectedIndex >= 0) {
            e.preventDefault();
            selectItem(items[selectedIndex]);
        } else if (e.key === "Escape") {
            closeList();
        }
    });

    document.addEventListener("click", function(e) {
        if (e.target !== input) closeList();
    });

    function fetchSuggestions(query) {
        fetch("/api/neighborhoods?q=" + encodeURIComponent(query))
            .then(r => r.json())
            .then(data => {
                items = data;
                selectedIndex = -1;
                renderList(data);
            })
            .catch(() => closeList());
    }

    function renderList(results) {
        listEl.innerHTML = "";
        if (!results.length) {
            closeList();
            return;
        }
        results.forEach((item, i) => {
            const div = document.createElement("div");
            div.className = "autocomplete-item";
            div.innerHTML = item.name + '<span class="city">' + item.city + '</span>';
            div.addEventListener("click", () => selectItem(item));
            div.addEventListener("mouseenter", () => {
                selectedIndex = i;
                highlightItem();
            });
            listEl.appendChild(div);
        });
        listEl.classList.add("active");
    }

    function selectItem(item) {
        input.value = item.name;
        cityInput.value = item.city;
        closeList();
    }

    function highlightItem() {
        const children = listEl.querySelectorAll(".autocomplete-item");
        children.forEach((el, i) => {
            el.classList.toggle("selected", i === selectedIndex);
        });
    }

    function closeList() {
        listEl.classList.remove("active");
        listEl.innerHTML = "";
        items = [];
        selectedIndex = -1;
    }

    // Form submit — disable button to prevent double-submit
    if (form) {
        form.addEventListener("submit", function() {
            const btn = document.getElementById("submitBtn");
            if (btn) {
                btn.disabled = true;
                btn.textContent = "Generating...";
            }
        });
    }
})();
