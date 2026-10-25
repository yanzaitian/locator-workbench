export async function decryptState(e,password){
 if(e.version!==1||e.iterations!==600000)throw Error('数据格式不支持');
 const b=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
 const salt=b(e.salt),iv=b(e.nonce);if(salt.length!==16||iv.length!==12)throw Error('数据格式错误');
 const source=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveKey']);
 const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:e.iterations,hash:'SHA-256'},source,{name:'AES-GCM',length:256},false,['decrypt']);
 return JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv,additionalData:new TextEncoder().encode('locator-state-v1')},key,b(e.ciphertext))));
}
