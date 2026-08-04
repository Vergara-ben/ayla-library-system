// Button Handler Functions - Use in all pages

// Navigation Functions
function navigateTo(page) {
  window.location.href = page;
}

function goBack() {
  window.history.back();
}

function goHome() {
  window.location.href = 'dashboardadmin.html';
}

// Modal Functions
function openModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.style.display = 'flex';
  }
}

function closeModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.style.display = 'none';
  }
}

function closeAllModals() {
  const modals = document.querySelectorAll('[id*="modal"]');
  modals.forEach(modal => {
    modal.style.display = 'none';
  });
}

// Form Functions
function resetForm(formId) {
  const form = document.getElementById(formId);
  if (form) {
    form.reset();
  }
}

function clearAllForms() {
  const forms = document.querySelectorAll('form');
  forms.forEach(form => form.reset());
}

// Search and Filter Functions
function searchTable(searchInputId, tableId) {
  const input = document.getElementById(searchInputId);
  const table = document.getElementById(tableId);
  
  if (!input || !table) return;
  
  const filter = input.value.toLowerCase();
  const rows = table.querySelectorAll('tbody tr');
  
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(filter) ? '' : 'none';
  });
}

function filterByStatus(statusValue, tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  
  const rows = table.querySelectorAll('tbody tr');
  rows.forEach(row => {
    const status = row.getAttribute('data-status');
    row.style.display = (status === statusValue || statusValue === 'all') ? '' : 'none';
  });
}

// Delete Functions with Confirmation
async function confirmDelete(itemName, deleteFunction, itemId) {
  const confirmed = confirm(`Are you sure you want to delete ${itemName}? This cannot be undone.`);
  
  if (confirmed) {
    try {
      const result = await deleteFunction(itemId);
      if (result) {
        showNotification('success', `${itemName} deleted successfully`);
        // Refresh table or reload
        setTimeout(() => location.reload(), 1000);
      } else {
        showNotification('error', `Failed to delete ${itemName}`);
      }
    } catch (error) {
      console.error('Delete error:', error);
      showNotification('error', 'An error occurred while deleting');
    }
  }
}

// Notification Functions
function showNotification(type, message, duration = 3000) {
  const notification = document.createElement('div');
  notification.className = `notification notification-${type}`;
  notification.style.cssText = `
    position: fixed;
    top: 20px;
    right: 20px;
    padding: 16px 24px;
    border-radius: 8px;
    z-index: 1000;
    animation: slideIn 0.3s ease-out;
    font-weight: 500;
    ${type === 'success' ? 'background-color: var(--success); color: var(--on-accent);' : ''}
    ${type === 'error' ? 'background-color: var(--danger); color: var(--on-accent);' : ''}
    ${type === 'warning' ? 'background-color: var(--warning); color: var(--on-accent);' : ''}
    ${type === 'info' ? 'background-color: var(--info); color: var(--on-accent);' : ''}
  `;
  notification.textContent = message;
  
  document.body.appendChild(notification);
  
  setTimeout(() => {
    notification.remove();
  }, duration);
}

// Data Table Functions
async function loadTableData(tableId, dataFunction, columns) {
  try {
    const data = await dataFunction();
    const table = document.getElementById(tableId);
    
    if (!table || !data) return;
    
    const tbody = table.querySelector('tbody');
    if (tbody) tbody.innerHTML = '';
    
    data.forEach(item => {
      const row = document.createElement('tr');
      row.setAttribute('data-id', item.id);
      row.setAttribute('data-status', item.status || '');
      
      columns.forEach(col => {
        const td = document.createElement('td');
        td.textContent = item[col] || '-';
        row.appendChild(td);
      });
      
      tbody.appendChild(row);
    });
  } catch (error) {
    console.error('Error loading table data:', error);
    showNotification('error', 'Failed to load data');
  }
}

// Export Data Functions
function exportTableToCSV(tableId, filename = 'export.csv') {
  const table = document.getElementById(tableId);
  if (!table) return;
  
  let csv = [];
  const rows = table.querySelectorAll('tr');
  
  rows.forEach(row => {
    const cols = row.querySelectorAll('td, th');
    const csvRow = Array.from(cols).map(col => {
      let text = col.textContent.trim();
      text = text.includes(',') ? `"${text}"` : text;
      return text;
    });
    csv.push(csvRow.join(','));
  });
  
  const csvContent = 'data:text/csv;charset=utf-8,' + csv.join('\n');
  const link = document.createElement('a');
  link.setAttribute('href', encodeURI(csvContent));
  link.setAttribute('download', filename);
  link.click();
  
  showNotification('success', 'Data exported successfully');
}

function printTable(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  
  const printWindow = window.open('', '', 'width=800,height=600');
  printWindow.document.write('<html><head><title>Print</title></head><body>');
  printWindow.document.write(table.outerHTML);
  printWindow.document.write('</body></html>');
  printWindow.document.close();
  printWindow.print();
  
  showNotification('success', 'Sent to printer');
}

// Pagination Functions
function createPagination(totalItems, itemsPerPage, pageCallback) {
  const totalPages = Math.ceil(totalItems / itemsPerPage);
  const pagination = document.createElement('div');
  pagination.className = 'pagination';
  pagination.style.cssText = `
    display: flex;
    gap: 8px;
    justify-content: center;
    margin-top: 20px;
  `;
  
  for (let i = 1; i <= totalPages; i++) {
    const btn = document.createElement('button');
    btn.textContent = i;
    btn.style.cssText = `
      padding: 8px 12px;
      border: 1px solid var(--border-color);
      background: var(--bg-subtle);
      color: var(--text-primary);
      cursor: pointer;
      border-radius: 4px;
    `;
    btn.onclick = () => pageCallback(i);
    pagination.appendChild(btn);
  }
  
  return pagination;
}

// Bulk Action Functions
function getBulkSelectedIds(checkboxSelector = '.bulk-checkbox') {
  const checkboxes = document.querySelectorAll(checkboxSelector);
  return Array.from(checkboxes)
    .filter(cb => cb.checked)
    .map(cb => cb.getAttribute('data-id'));
}

async function bulkDelete(selectedIds, deleteFunction, itemName = 'items') {
  if (selectedIds.length === 0) {
    showNotification('warning', 'No items selected');
    return;
  }
  
  const confirmed = confirm(`Delete ${selectedIds.length} ${itemName}? This cannot be undone.`);
  
  if (confirmed) {
    let deletedCount = 0;
    
    for (const id of selectedIds) {
      try {
        const result = await deleteFunction(id);
        if (result) deletedCount++;
      } catch (error) {
        console.error('Error deleting:', error);
      }
    }
    
    showNotification('success', `Deleted ${deletedCount}/${selectedIds.length} ${itemName}`);
    setTimeout(() => location.reload(), 1000);
  }
}

// Form Validation Functions
function validateEmail(email) {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return emailRegex.test(email);
}

function validatePhone(phone) {
  const phoneRegex = /^[\d\s\-\+\(\)]{10,}$/;
  return phoneRegex.test(phone);
}

function validateFormField(fieldId, validationType = 'text') {
  const field = document.getElementById(fieldId);
  if (!field) return false;
  
  const value = field.value.trim();
  
  if (!value) {
    field.style.borderColor = 'var(--danger)';
    return false;
  }
  
  if (validationType === 'email' && !validateEmail(value)) {
    field.style.borderColor = 'var(--danger)';
    return false;
  }
  
  if (validationType === 'phone' && !validatePhone(value)) {
    field.style.borderColor = 'var(--danger)';
    return false;
  }
  
  field.style.borderColor = 'var(--success)';
  return true;
}

// Auto-complete/Suggestions
function setupAutoComplete(inputId, suggestionList) {
  const input = document.getElementById(inputId);
  if (!input) return;
  
  const suggestionContainer = document.createElement('div');
  suggestionContainer.style.cssText = `
    position: absolute;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    max-height: 200px;
    overflow-y: auto;
    width: 100%;
    z-index: 100;
    display: none;
  `;
  
  input.parentElement.style.position = 'relative';
  input.parentElement.appendChild(suggestionContainer);
  
  input.addEventListener('input', (e) => {
    const value = e.target.value.toLowerCase();
    const filtered = suggestionList.filter(item => 
      item.toLowerCase().includes(value)
    );
    
    suggestionContainer.innerHTML = '';
    
    if (filtered.length > 0 && value) {
      filtered.forEach(item => {
        const div = document.createElement('div');
        div.textContent = item;
        div.style.cssText = `
          padding: 8px 12px;
          cursor: pointer;
          border-bottom: 1px solid var(--border-color);
        `;
        div.onmouseover = () => div.style.backgroundColor = 'var(--bg-subtle)';
        div.onmouseout = () => div.style.backgroundColor = 'transparent';
        div.onclick = () => {
          input.value = item;
          suggestionContainer.style.display = 'none';
        };
        suggestionContainer.appendChild(div);
      });
      suggestionContainer.style.display = 'block';
    } else {
      suggestionContainer.style.display = 'none';
    }
  });
}

// Toggle Functions
function toggleClass(elementId, className) {
  const element = document.getElementById(elementId);
  if (element) {
    element.classList.toggle(className);
  }
}

function toggleVisibility(elementId) {
  const element = document.getElementById(elementId);
  if (element) {
    element.style.display = element.style.display === 'none' ? 'block' : 'none';
  }
}

// Timestamp/Date Formatting
function formatDate(date) {
  return new Date(date).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric'
  });
}

function formatDateTime(date) {
  return new Date(date).toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  });
}

// Initialize Page - Called on DOMContentLoaded
function initializePage() {
  console.log('Page initialized with button handlers');
  setupCloseModalOnOutsideClick();
  setupCloseModalOnEscKey();
}

// Close modal when clicking outside
function setupCloseModalOnOutsideClick() {
  document.addEventListener('click', (e) => {
    const modals = document.querySelectorAll('[role="dialog"]');
    modals.forEach(modal => {
      if (e.target === modal) {
        modal.style.display = 'none';
      }
    });
  });
}

// Close modal on Escape key
function setupCloseModalOnEscKey() {
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllModals();
    }
  });
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initializePage);
} else {
  initializePage();
}
