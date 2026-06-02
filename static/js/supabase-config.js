// Supabase Configuration
// Replace these with your actual Supabase credentials from https://supabase.com

const SUPABASE_URL = 'https://your-project-id.supabase.co';
const SUPABASE_ANON_KEY = 'your-anon-key-here';

// Initialize Supabase client
let supabase = null;

async function initSupabase() {
  if (!supabase) {
    // Load Supabase library dynamically
    const script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.0/dist/main.min.js';
    script.async = true;
    
    script.onload = () => {
      supabase = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
      console.log('Supabase initialized successfully');
    };
    
    script.onerror = () => {
      console.error('Failed to load Supabase library');
    };
    
    document.head.appendChild(script);
  }
  return supabase;
}

// Wait for Supabase to be ready
async function whenSupabaseReady() {
  return new Promise((resolve) => {
    const checkInterval = setInterval(() => {
      if (supabase) {
        clearInterval(checkInterval);
        resolve(supabase);
      }
    }, 100);
    // Timeout after 10 seconds
    setTimeout(() => {
      clearInterval(checkInterval);
      console.error('Supabase initialization timeout');
      resolve(null);
    }, 10000);
  });
}

// Common database functions

// Patrons
async function addPatron(patronData) {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data, error } = await client
    .from('patrons')
    .insert([patronData])
    .select();
  
  if (error) {
    console.error('Error adding patron:', error);
    return null;
  }
  return data[0];
}

async function getPatrons() {
  const client = await whenSupabaseReady();
  if (!client) return [];
  
  const { data, error } = await client
    .from('patrons')
    .select('*');
  
  if (error) {
    console.error('Error fetching patrons:', error);
    return [];
  }
  return data;
}

async function updatePatron(patronId, updates) {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data, error } = await client
    .from('patrons')
    .update(updates)
    .eq('id', patronId)
    .select();
  
  if (error) {
    console.error('Error updating patron:', error);
    return null;
  }
  return data[0];
}

async function deletePatron(patronId) {
  const client = await whenSupabaseReady();
  if (!client) return false;
  
  const { error } = await client
    .from('patrons')
    .delete()
    .eq('id', patronId);
  
  if (error) {
    console.error('Error deleting patron:', error);
    return false;
  }
  return true;
}

// Books
async function addBook(bookData) {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data, error } = await client
    .from('books')
    .insert([bookData])
    .select();
  
  if (error) {
    console.error('Error adding book:', error);
    return null;
  }
  return data[0];
}

async function getBooks() {
  const client = await whenSupabaseReady();
  if (!client) return [];
  
  const { data, error } = await client
    .from('books')
    .select('*');
  
  if (error) {
    console.error('Error fetching books:', error);
    return [];
  }
  return data;
}

async function updateBook(bookId, updates) {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data, error } = await client
    .from('books')
    .update(updates)
    .eq('id', bookId)
    .select();
  
  if (error) {
    console.error('Error updating book:', error);
    return null;
  }
  return data[0];
}

// Transactions
async function addTransaction(transactionData) {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data, error } = await client
    .from('transactions')
    .insert([transactionData])
    .select();
  
  if (error) {
    console.error('Error adding transaction:', error);
    return null;
  }
  return data[0];
}

async function getTransactions() {
  const client = await whenSupabaseReady();
  if (!client) return [];
  
  const { data, error } = await client
    .from('transactions')
    .select('*')
    .order('created_at', { ascending: false });
  
  if (error) {
    console.error('Error fetching transactions:', error);
    return [];
  }
  return data;
}

// Access Logs
async function addAccessLog(logData) {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data, error } = await client
    .from('access_logs')
    .insert([logData])
    .select();
  
  if (error) {
    console.error('Error adding access log:', error);
    return null;
  }
  return data[0];
}

async function getAccessLogs() {
  const client = await whenSupabaseReady();
  if (!client) return [];
  
  const { data, error } = await client
    .from('access_logs')
    .select('*')
    .order('created_at', { ascending: false });
  
  if (error) {
    console.error('Error fetching access logs:', error);
    return [];
  }
  return data;
}

// Authentication
async function adminSignIn(email, password) {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data, error } = await client.auth.signInWithPassword({
    email,
    password,
  });
  
  if (error) {
    console.error('Error signing in:', error);
    return null;
  }
  return data.user;
}

async function adminSignOut() {
  const client = await whenSupabaseReady();
  if (!client) return false;
  
  const { error } = await client.auth.signOut();
  
  if (error) {
    console.error('Error signing out:', error);
    return false;
  }
  return true;
}

async function getCurrentUser() {
  const client = await whenSupabaseReady();
  if (!client) return null;
  
  const { data: { user } } = await client.auth.getUser();
  return user;
}

// Initialize on page load
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSupabase);
} else {
  initSupabase();
}
