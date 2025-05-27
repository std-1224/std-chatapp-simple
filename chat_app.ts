// BIG FAT WARNING: to avoid the complexity of npm, this typescript is compiled in the browser
// there's currently no static type checking

import { parse as marked } from 'https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.0/lib/marked.esm.js'

// Define types for our elements
interface HTMLElements {
  conversation: HTMLElement;
  promptInput: HTMLInputElement;
  spinner: HTMLElement;
  error: HTMLElement;
  form: HTMLFormElement;
}

// Define the Message interface
interface Message {
  role: 'user' | 'model';
  content: string;
  timestamp: string;
}

// Get DOM elements with type safety
function getElements(): HTMLElements {
  const conversation = document.getElementById('conversation');
  const promptInput = document.getElementById('prompt-input') as HTMLInputElement;
  const spinner = document.getElementById('spinner');
  const error = document.getElementById('error');
  const form = document.querySelector('form');

  if (!conversation || !promptInput || !spinner || !error || !form) {
    throw new Error('Required DOM elements not found');
  }

  return {
    conversation,
    promptInput,
    spinner,
    error,
    form
  };
}

// Initialize elements
const elements = getElements();

// stream the response and render messages as each chunk is received
// data is sent as newline-delimited JSON
async function onFetchResponse(response: Response): Promise<void> {
  let text = '';
  const decoder = new TextDecoder();
  
  if (!response.ok) {
    const text = await response.text();
    console.error(`Unexpected response: ${response.status}`, { response, text });
    throw new Error(`Unexpected response: ${response.status}`);
  }

  if (!response.body) {
    throw new Error('Response body is null');
  }

  const reader = response.body.getReader();
  
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      text += decoder.decode(value);
      addMessages(text);
      elements.spinner.classList.remove('active');
    }
    
    addMessages(text);
    elements.promptInput.disabled = false;
    elements.promptInput.focus();
  } finally {
    reader.releaseLock();
  }
}

// take raw response text and render messages into the `#conversation` element
function addMessages(responseText: string): void {
  const lines = responseText.split('\n');
  const messages: Message[] = lines
    .filter(line => line.length > 1)
    .map(j => JSON.parse(j));

  for (const message of messages) {
    const { timestamp, role, content } = message;
    const id = `msg-${timestamp}`;
    let msgDiv = document.getElementById(id);

    if (!msgDiv) {
      msgDiv = document.createElement('div');
      msgDiv.id = id;
      msgDiv.title = `${role} at ${timestamp}`;
      msgDiv.classList.add('border-top', 'pt-2', role);
      elements.conversation.appendChild(msgDiv);
    }

    // Use marked to parse markdown content
    msgDiv.innerHTML = marked(content);
  }

  window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

function onError(error: unknown): void {
  console.error(error);
  elements.error.classList.remove('d-none');
  elements.spinner.classList.remove('active');
}

async function onSubmit(e: SubmitEvent): Promise<void> {
  e.preventDefault();
  elements.spinner.classList.add('active');
  
  const form = e.target as HTMLFormElement;
  const body = new FormData(form);

  elements.promptInput.value = '';
  elements.promptInput.disabled = true;

  try {
    const response = await fetch('/chat/', { method: 'POST', body });
    await onFetchResponse(response);
  } catch (error) {
    onError(error);
  }
}

// Initialize event listeners
elements.form.addEventListener('submit', (e: Event) => {
  onSubmit(e as SubmitEvent).catch(onError);
});

// Load messages on page load
fetch('/chat/')
  .then(onFetchResponse)
  .catch(onError);