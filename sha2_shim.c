/* Debian's libpython3.12.a ships _sha2 but not the HACL* sources it needs
 * (they only exist inside the _hashlib .so).  Provide the 12 missing
 * symbols on top of OpenSSL's libcrypto so hashlib.sha224/256/384/512 work
 * in the static binary. */
#include <openssl/evp.h>
#include <stdint.h>
#include <stdlib.h>

typedef struct { EVP_MD_CTX *c; } st;

static st *mk(const EVP_MD *md) {
    st *s = malloc(sizeof *s);
    if (!s) return NULL;
    s->c = EVP_MD_CTX_new();
    if (!s->c || !EVP_DigestInit_ex(s->c, md, NULL)) { free(s); return NULL; }
    return s;
}
static st *cp(st *o) {
    st *s = malloc(sizeof *s);
    if (!s) return NULL;
    s->c = EVP_MD_CTX_new();
    if (!s->c || !EVP_MD_CTX_copy_ex(s->c, o->c)) { free(s); return NULL; }
    return s;
}
static void up(st *s, uint8_t *b, uint32_t n) { EVP_DigestUpdate(s->c, b, n); }
static void dg(st *s, uint8_t *out) {
    EVP_MD_CTX *t = EVP_MD_CTX_new();
    EVP_MD_CTX_copy_ex(t, s->c);
    EVP_DigestFinal_ex(t, out, NULL);
    EVP_MD_CTX_free(t);
}
static void fr(st *s) { if (s) { EVP_MD_CTX_free(s->c); free(s); } }


st *python_hashlib_Hacl_Hash_SHA2_malloc_224(void) { return mk(EVP_sha224()); }
st *python_hashlib_Hacl_Hash_SHA2_malloc_256(void) { return mk(EVP_sha256()); }
st *python_hashlib_Hacl_Hash_SHA2_malloc_384(void) { return mk(EVP_sha384()); }
st *python_hashlib_Hacl_Hash_SHA2_malloc_512(void) { return mk(EVP_sha512()); }
st *python_hashlib_Hacl_Hash_SHA2_copy_256(st *s) { return cp(s); }
st *python_hashlib_Hacl_Hash_SHA2_copy_512(st *s) { return cp(s); }
void python_hashlib_Hacl_Hash_SHA2_update_256(st *s, uint8_t *b, uint32_t n) { up(s, b, n); }
void python_hashlib_Hacl_Hash_SHA2_update_512(st *s, uint8_t *b, uint32_t n) { up(s, b, n); }
void python_hashlib_Hacl_Hash_SHA2_digest_256(st *s, uint8_t *o) { dg(s, o); }
void python_hashlib_Hacl_Hash_SHA2_digest_512(st *s, uint8_t *o) { dg(s, o); }
void python_hashlib_Hacl_Hash_SHA2_free_256(st *s) { fr(s); }
void python_hashlib_Hacl_Hash_SHA2_free_512(st *s) { fr(s); }
