/* wbc_json.h -- materialized from the guarded block inside wbc_json.c
 * (see that file's comment); interface unchanged. */
#ifndef WBC_JSON_H
#define WBC_JSON_H

#include <stddef.h>

typedef struct JVal JVal;

JVal *jparse(const char *buf, size_t len);   /* NULL = syntax error */
const char *jparse_err(void);                /* text of the last error */
void jfree(JVal *v);
int jis_obj(const JVal *v);
int jis_arr(const JVal *v);
int jis_str(const JVal *v);
int jis_num(const JVal *v);
int jis_null(const JVal *v);
int jis_bool(const JVal *v);
const JVal *jget(const JVal *obj, const char *key);   /* NULL if absent */
size_t jlen(const JVal *arr);
const JVal *jat(const JVal *arr, size_t i);
const char *jstr(const JVal *s);            /* UTF-8, NUL-terminated */
int jbool(const JVal *v);                   /* 1/0; 0 for non-bool */
int jnum_is_int(const JVal *v);             /* integer and inside int64 */
long long jint(const JVal *v);              /* 0 for non-integer */
double jnum(const JVal *v);                 /* 0.0 for non-number */
/* object key iteration, in document order */
size_t jobj_len(const JVal *obj);
const char *jobj_key(const JVal *obj, size_t i);
const JVal *jobj_val(const JVal *obj, size_t i);

/* ---- writer ---- */
typedef struct JW JW;
JW *jw_new(void);
void jw_free(JW *jw);
char *jw_take(JW *jw, size_t *len);         /* detaches the buffer */
void jw_raw(JW *jw, const char *s);
void jw_str(JW *jw, const char *s);         /* quotes + escapes */
void jw_int(JW *jw, long long v);
void jw_double(JW *jw, double v);           /* %.17g, exact read-back */
void jw_bool(JW *jw, int v);
void jw_null(JW *jw);
void jw_obj_begin(JW *jw);
void jw_obj_end(JW *jw);
void jw_arr_begin(JW *jw);
void jw_arr_end(JW *jw);
void jw_key(JW *jw, const char *key);       /* "key": */

#endif /* WBC_JSON_H */
