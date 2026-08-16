document.addEventListener('DOMContentLoaded', function () {
    const congregationSelect = document.querySelector('#id_congregation');
    const territorySelect = document.querySelector('#id_filtered_territories');

    if (congregationSelect && territorySelect) {
        congregationSelect.addEventListener('change', function () {
            const congId = this.value;
            if (!congId) return;

            fetch(`/admin/deck/deck/territories-by-congregation/?congregation_id=${congId}`)
                .then(response => response.json())
                .then(data => {
                    territorySelect.innerHTML = '';
                    data.forEach(t => {
                        const option = document.createElement('option');
                        option.value = t.id;
                        option.textContent = t.address;
                        territorySelect.appendChild(option);
                    });
                });
        });
    }
});
