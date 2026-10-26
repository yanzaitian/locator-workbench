export async function decryptState(e,password){
 if(e.version!==1||e.iterations!==600000)throw Error('数据格式不支持');
 const b=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
 const salt=b(e.salt),iv=b(e.nonce);if(salt.length!==16||iv.length!==12)throw Error('数据格式错误');
 const source=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveKey']);
 const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:e.iterations,hash:'SHA-256'},source,{name:'AES-GCM',length:256},false,['decrypt']);
 return JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv,additionalData:new TextEncoder().encode('locator-state-v1')},key,b(e.ciphertext))));
}

export async function encryptState(value,password){
 if(typeof password!=='string'||password.length<12)throw Error('访问密码至少12个字符');
 const salt=crypto.getRandomValues(new Uint8Array(16)),nonce=crypto.getRandomValues(new Uint8Array(12));
 const source=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveKey']);
 const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:600000,hash:'SHA-256'},source,{name:'AES-GCM',length:256},false,['encrypt']);
 const ciphertext=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce,additionalData:new TextEncoder().encode('locator-state-v1')},key,new TextEncoder().encode(JSON.stringify(value))));
 const b64=bytes=>{let text='';for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(text)};
 return {version:1,iterations:600000,salt:b64(salt),nonce:b64(nonce),ciphertext:b64(ciphertext)};
}
