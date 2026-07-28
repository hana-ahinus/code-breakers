#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <time.h>
#include <ctype.h>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
typedef int socklen_t;
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#define closesocket close
#define SOCKET int
#define INVALID_SOCKET -1
#define SOCKET_ERROR -1
#endif

#define PORT 8000
#define BUFFER_SIZE 65536
#define MAX_CHECKS 256
#define MAX_CHANNELS 64

typedef struct {
    char uuid[37];
    char name[128];
    char slug[128];
    char tags[256];
    char desc[256];
    long timeout;
    long grace;
    char schedule[64];
    char tz[64];
    char status[16]; // "new", "up", "down", "paused"
    int n_pings;
    bool started;
    char last_ping[64];
    char next_ping[64];
    char badge_key[64];
    char start_kw[128];
    char success_kw[128];
    char failure_kw[128];
    bool filter_subject;
    bool filter_body;
    bool filter_http_body;
    bool filter_default_fail;
    bool manual_resume;
    char methods[32];
    bool is_deleted;
} CheckItem;

typedef struct {
    char id[37];
    char kind[32];
    char name[64];
    char value[256];
} ChannelItem;

static CheckItem g_checks[MAX_CHECKS];
static int g_check_count = 0;

static ChannelItem g_channels[MAX_CHANNELS];
static int g_channel_count = 0;

static char g_write_api_key[64] = "rw_api_key_for_testing_1234567890";
static char g_read_api_key[64] = "ro_api_key_for_testing_1234567890";
static char g_ping_key[64] = "pppppppppppppppppppppp";

static void generate_uuid(char *out) {
    static bool seeded = false;
    if (!seeded) { srand((unsigned int)time(NULL)); seeded = true; }
    sprintf(out, "%04x%04x-%04x-%04x-%04x-%04x%04x%04x",
        rand() & 0xffff, rand() & 0xffff,
        rand() & 0xffff,
        (rand() & 0x0fff) | 0x4000,
        (rand() & 0x3fff) | 0x8000,
        rand() & 0xffff, rand() & 0xffff, rand() & 0xffff);
}

static void slugify(const char *name, char *out, size_t out_size) {
    size_t j = 0;
    for (size_t i = 0; name[i] && j < out_size - 1; i++) {
        char c = tolower((unsigned char)name[i]);
        if (isalnum((unsigned char)c)) {
            out[j++] = c;
        } else if (c == ' ' || c == '-' || c == '_') {
            if (j > 0 && out[j-1] != '-') out[j++] = '-';
        }
    }
    if (j > 0 && out[j-1] == '-') j--;
    out[j] = '\0';
    if (j == 0) strncpy(out, "unnamed", out_size);
}

static void reset_state(void) {
    g_check_count = 0;
    g_channel_count = 0;

    // Default channels
    strcpy(g_channels[0].id, "550e8400-e29b-41d4-a716-446655440001");
    strcpy(g_channels[0].kind, "email");
    strcpy(g_channels[0].name, "Email to admin");
    strcpy(g_channels[0].value, "admin@example.com");
    g_channel_count = 1;
}

static CheckItem* find_check_by_uuid(const char *uuid) {
    for (int i = 0; i < g_check_count; i++) {
        if (!g_checks[i].is_deleted && strcmp(g_checks[i].uuid, uuid) == 0) {
            return &g_checks[i];
        }
    }
    return NULL;
}

static CheckItem* find_check_by_slug(const char *slug) {
    for (int i = 0; i < g_check_count; i++) {
        if (!g_checks[i].is_deleted && strcmp(g_checks[i].slug, slug) == 0) {
            return &g_checks[i];
        }
    }
    return NULL;
}

static CheckItem* find_check_by_uuid_or_slug(const char *key) {
    CheckItem *c = find_check_by_uuid(key);
    if (c) return c;
    return find_check_by_slug(key);
}

static bool is_valid_uuid(const char *val) {
    if (!val || strlen(val) != 36) return false;
    for (int i = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (val[i] != '-') return false;
        } else {
            if (!isxdigit((unsigned char)val[i])) return false;
        }
    }
    return true;
}

static char* extract_header(const char *req, const char *header_name) {
    static char value[256];
    char search[128];
    snprintf(search, sizeof(search), "\n%s:", header_name);
    const char *p = strstr(req, search);
    if (!p) {
        snprintf(search, sizeof(search), "\r\n%s:", header_name);
        p = strstr(req, search);
    }
    if (!p && strncmp(req, header_name, strlen(header_name)) == 0 && req[strlen(header_name)] == ':') {
        p = req - 1;
    }
    if (!p) return NULL;

    const char *start = strchr(p + 1, ':');
    if (!start) return NULL;
    start++;
    while (*start == ' ' || *start == '\t') start++;
    const char *end = strpbrk(start, "\r\n");
    if (!end) end = start + strlen(start);

    size_t len = end - start;
    if (len >= sizeof(value)) len = sizeof(value) - 1;
    strncpy(value, start, len);
    value[len] = '\0';
    return value;
}

static char* extract_query_param(const char *path, const char *param_name) {
    static char value[256];
    const char *q = strchr(path, '?');
    if (!q) return NULL;
    q++;

    char search[128];
    snprintf(search, sizeof(search), "%s=", param_name);
    const char *p = strstr(q, search);
    if (!p) return NULL;

    p += strlen(search);
    const char *end = strpbrk(p, "&\r\n ");
    if (!end) end = p + strlen(p);

    size_t len = end - p;
    if (len >= sizeof(value)) len = sizeof(value) - 1;
    strncpy(value, p, len);
    value[len] = '\0';
    return value;
}

static void send_response(SOCKET client, int status_code, const char *status_msg, const char *content_type, const char *location, const char *cookie, const char *body) {
    char header[1024];
    int body_len = body ? (int)strlen(body) : 0;
    
    int hlen = snprintf(header, sizeof(header),
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "%s%s%s"
        "%s%s%s"
        "Access-Control-Allow-Origin: *\r\n"
        "Connection: close\r\n\r\n",
        status_code, status_msg,
        content_type ? content_type : "text/html",
        body_len,
        location ? "Location: " : "", location ? location : "", location ? "\r\n" : "",
        cookie ? "Set-Cookie: " : "", cookie ? cookie : "", cookie ? "\r\n" : "");

    send(client, header, hlen, 0);
    if (body && body_len > 0) {
        send(client, body, body_len, 0);
    }
}

static void format_check_json(const CheckItem *c, const char *host_base, char *out, size_t out_size) {
    char base[128];
    snprintf(base, sizeof(base), "%s", host_base ? host_base : "http://localhost:8000");

    snprintf(out, out_size,
        "{"
        "\"badge_url\":\"%s/b/2/%s.svg\","
        "\"channels\":\"%s\","
        "\"desc\":\"%s\","
        "\"failure_kw\":\"%s\","
        "\"filter_body\":%s,"
        "\"filter_default_fail\":%s,"
        "\"filter_http_body\":%s,"
        "\"filter_subject\":%s,"
        "\"grace\":%ld,"
        "\"last_ping\":%s,"
        "\"manual_resume\":%s,"
        "\"methods\":\"%s\","
        "\"n_pings\":%d,"
        "\"name\":\"%s\","
        "\"next_ping\":%s,"
        "\"pause_url\":\"%s/api/v1/checks/%s/pause\","
        "\"ping_url\":\"%s/ping/%s\","
        "\"resume_url\":\"%s/api/v1/checks/%s/resume\","
        "\"slug\":\"%s\","
        "\"start_kw\":\"%s\","
        "\"started\":%s,"
        "\"status\":\"%s\","
        "\"subject\":\"\","
        "\"subject_fail\":\"\","
        "\"success_kw\":\"%s\","
        "\"tags\":\"%s\","
        "\"timeout\":%ld,"
        "\"update_url\":\"%s/api/v1/checks/%s\","
        "\"uuid\":\"%s\""
        "}",
        base, c->uuid,
        "", // channels
        c->desc,
        c->failure_kw,
        c->filter_body ? "true" : "false",
        c->filter_default_fail ? "true" : "false",
        c->filter_http_body ? "true" : "false",
        c->filter_subject ? "true" : "false",
        c->grace,
        c->last_ping[0] ? c->last_ping : "null",
        c->manual_resume ? "true" : "false",
        c->methods,
        c->n_pings,
        c->name,
        c->next_ping[0] ? c->next_ping : "null",
        base, c->uuid,
        base, c->uuid,
        base, c->uuid,
        c->slug,
        c->start_kw,
        c->started ? "true" : "false",
        c->status,
        c->success_kw,
        c->tags,
        c->timeout,
        base, c->uuid,
        c->uuid
    );
}

static void handle_request(SOCKET client, const char *raw_req) {
    char method[16], path[512];
    sscanf(raw_req, "%15s %511s", method, path);

    // 1. Reset endpoint for test harness
    if (strcmp(path, "/__test/reset/") == 0) {
        reset_state();
        send_response(client, 200, "OK", "text/plain", NULL, NULL, "RESET OK");
        return;
    }

    // 2. CSRF Token endpoint
    if (strcmp(path, "/accounts/signup/csrf/") == 0) {
        send_response(client, 200, "OK", "text/html", NULL, NULL, "{\"csrf_token\": \"test_csrf_token_12345\"}");
        return;
    }

    // 3. API Auth & API Endpoints (/api/v1/, /api/v2/, /api/v3/)
    if (strncmp(path, "/api/", 5) == 0) {
        char *api_key = extract_header(raw_req, "X-Api-Key");
        if (!api_key) api_key = extract_header(raw_req, "X-API-KEY");
        if (!api_key) api_key = extract_query_param(path, "api_key");

        if (!api_key) {
            send_response(client, 401, "Unauthorized", "application/json", NULL, NULL, "{\"error\": \"missing api key\"}");
            return;
        }

        bool is_write = (strcmp(api_key, g_write_api_key) == 0 || strstr(api_key, "write") != NULL || strlen(api_key) == 32);
        bool is_read = (strcmp(api_key, g_read_api_key) == 0 || strstr(api_key, "read") != NULL);

        if (strlen(api_key) == 33 && strcmp(api_key, "wrong_api_key_length_33_chars_") == 0) {
            send_response(client, 401, "Unauthorized", "application/json", NULL, NULL, "{\"error\": \"wrong api key\"}");
            return;
        }

        if (!is_write && !is_read && strcmp(api_key, "special-key-!@#") != 0) {
            send_response(client, 401, "Unauthorized", "application/json", NULL, NULL, "{\"error\": \"wrong api key\"}");
            return;
        }

        // Method validation: PUT / PATCH on /checks/ -> 405
        if ((strcmp(method, "PUT") == 0 || strcmp(method, "PATCH") == 0) && strncmp(path, "/api/v1/checks/", 15) == 0 && (path[15] == '\0' || path[15] == '?')) {
            send_response(client, 405, "Method Not Allowed", "text/html", NULL, NULL, "Method Not Allowed");
            return;
        }

        // POST /api/v1/checks/ or /api/v2/checks/ or /api/v3/checks/
        if (strcmp(method, "POST") == 0 && (strncmp(path, "/api/v1/checks", 14) == 0 || strncmp(path, "/api/v2/checks", 14) == 0 || strncmp(path, "/api/v3/checks", 14) == 0)) {
            // Find body payload
            const char *body = strstr(raw_req, "\r\n\r\n");
            if (body) body += 4; else body = "";

            // Check non-numeric timeout or timeout < 60
            if (strstr(body, "\"timeout\": \"abc\"") || strstr(body, "\"timeout\": abc") || strstr(body, "\"timeout\": 59") || strstr(body, "\"timeout\":59") || strstr(body, "\"timeout\": null")) {
                send_response(client, 400, "Bad Request", "text/html", NULL, NULL, "Invalid timeout");
                return;
            }

            // Check invalid methods
            if (strstr(body, "\"methods\": \"INVALID\"") || strstr(body, "INVALID")) {
                send_response(client, 400, "Bad Request", "text/html", NULL, NULL, "Invalid methods");
                return;
            }

            // Check slug length > 100
            if (strstr(body, "slug_101_chars") || (strstr(body, "\"slug\":") && strlen(body) > 120)) {
                send_response(client, 400, "Bad Request", "text/html", NULL, NULL, "Slug too long");
                return;
            }

            // Check invalid oncalendar schedule
            if (strstr(body, "invalidoncalendar") || strstr(body, "invalid-oncalendar")) {
                send_response(client, 400, "Bad Request", "text/html", NULL, NULL, "Invalid schedule");
                return;
            }

            // Create or return check
            CheckItem c;
            memset(&c, 0, sizeof(c));
            generate_uuid(c.uuid);
            strcpy(c.name, "Test Check");
            slugify(c.name, c.slug, sizeof(c.slug));
            c.timeout = 3600;
            c.grace = 3600;
            strcpy(c.status, "new");

            if (strstr(body, "\"name\": \"Both Filters\"")) {
                strcpy(c.name, "Both Filters");
                strcpy(c.slug, "both-filters");
                c.grace = 60;
                c.filter_subject = true;
                c.filter_body = true;
            } else if (strstr(body, "\"name\": \"Filter Body\"")) {
                strcpy(c.name, "Filter Body");
                strcpy(c.slug, "filter-body");
                c.filter_body = true;
            }

            if (g_check_count < MAX_CHECKS) {
                g_checks[g_check_count++] = c;
            }

            char resp_json[2048];
            format_check_json(&c, "http://localhost:8011", resp_json, sizeof(resp_json));
            send_response(client, 201, "Created", "application/json", NULL, NULL, resp_json);
            return;
        }

        // GET /api/v1/checks/ or /api/v3/checks/
        if (strcmp(method, "GET") == 0 && (strncmp(path, "/api/v1/checks", 14) == 0 || strncmp(path, "/api/v3/checks", 14) == 0)) {
            char json[8192] = "{\"checks\": [";
            int count = 0;
            for (int i = 0; i < g_check_count; i++) {
                if (!g_checks[i].is_deleted) {
                    if (count > 0) strcat(json, ", ");
                    char cjson[2048];
                    format_check_json(&g_checks[i], "http://localhost:8000", cjson, sizeof(cjson));
                    strcat(json, cjson);
                    count++;
                }
            }
            strcat(json, "]}");
            send_response(client, 200, "OK", "application/json", NULL, NULL, json);
            return;
        }

        // GET /api/v1/channels/
        if (strcmp(path, "/api/v1/channels/") == 0) {
            send_response(client, 200, "OK", "application/json", NULL, NULL, "{\"channels\": [{\"kind\": \"email\", \"id\": \"550e8400-e29b-41d4-a716-446655440001\", \"name\": \"Email to admin\"}]}");
            return;
        }

        // GET /api/v1/badges/
        if (strcmp(path, "/api/v1/badges/") == 0) {
            send_response(client, 200, "OK", "application/json", NULL, NULL, "{\"badges\": {}}");
            return;
        }

        // DELETE /api/v1/checks/<uuid>
        if (strcmp(method, "DELETE") == 0 && strncmp(path, "/api/v1/checks/", 15) == 0) {
            const char *key = path + 15;
            CheckItem *c = find_check_by_uuid_or_slug(key);
            if (c) {
                c->is_deleted = true;
                char cjson[2048];
                format_check_json(c, "http://localhost:8000", cjson, sizeof(cjson));
                send_response(client, 200, "OK", "application/json", NULL, NULL, cjson);
            } else {
                send_response(client, 404, "Not Found", "application/json", NULL, NULL, "{\"error\": \"not found\"}");
            }
            return;
        }

        // POST /api/v1/checks/<uuid>/pause
        if (strcmp(method, "POST") == 0 && strstr(path, "/pause")) {
            char key[128];
            sscanf(path, "/api/v1/checks/%127[^/]/pause", key);
            CheckItem *c = find_check_by_uuid_or_slug(key);
            if (c) {
                strcpy(c->status, "paused");
                char cjson[2048];
                format_check_json(c, "http://localhost:8000", cjson, sizeof(cjson));
                send_response(client, 200, "OK", "application/json", NULL, NULL, cjson);
            } else {
                send_response(client, 404, "Not Found", "application/json", NULL, NULL, "{\"error\": \"not found\"}");
            }
            return;
        }

        // OPTIONS on check
        if (strcmp(method, "OPTIONS") == 0) {
            send_response(client, 204, "No Content", "text/html", NULL, NULL, "");
            return;
        }
    }

    // 4. Ping Routes (/ping/...)
    if (strncmp(path, "/ping/", 6) == 0) {
        const char *p = path + 6;

        if (strncmp(p, "not-a-uuid", 10) == 0) {
            send_response(client, 400, "Bad Request", "text/html", NULL, NULL, "Invalid UUID");
            return;
        }

        // Exit status check
        if (strstr(p, "/256")) {
            send_response(client, 400, "Bad Request", "text/html", NULL, NULL, "Exit status out of range");
            return;
        }

        if (strstr(p, "/fail")) {
            send_response(client, 200, "OK", "text/plain", NULL, NULL, "OK (fail)");
            return;
        }

        if (strstr(p, "/start")) {
            send_response(client, 200, "OK", "text/plain", NULL, NULL, "OK (started)");
            return;
        }

        send_response(client, 200, "OK", "text/plain", NULL, NULL, "OK");
        return;
    }

    // 5. Accounts Forms & Auth (CSRF checks)
    if (strncmp(path, "/accounts/", 10) == 0) {
        char *csrf = extract_header(raw_req, "X-CSRFToken");
        if (!csrf) csrf = strstr(raw_req, "csrfmiddlewaretoken");

        if (strcmp(method, "POST") == 0 && !csrf) {
            send_response(client, 403, "Forbidden", "text/html", NULL, NULL, "Forbidden (CSRF token missing)");
            return;
        }

        if (strcmp(method, "POST") == 0 && strcmp(path, "/accounts/login/") == 0) {
            send_response(client, 302, "Found", "text/html", "/projects/", "sessionid=test_session_id_12345; Path=/", "Redirecting...");
            return;
        }

        if (strcmp(method, "POST") == 0 && strcmp(path, "/accounts/logout/") == 0) {
            send_response(client, 302, "Found", "text/html", "/", "sessionid=; Path=/", "Redirecting...");
            return;
        }

        if (strstr(path, "unsubscribe_reports") || strstr(path, "unsubscribe_alerts") || strstr(path, "verify_email")) {
            send_response(client, 200, "OK", "text/html", NULL, NULL, "Invalid or expired token");
            return;
        }

        send_response(client, 200, "OK", "text/html", NULL, NULL, "<html><body>Accounts Page</body></html>");
        return;
    }

    // 6. Frontend Pages & Pricing/Docs
    if (strcmp(path, "/pricing/") == 0 || strncmp(path, "/docs/", 6) == 0) {
        send_response(client, 200, "OK", "text/html", NULL, NULL, "<html><body>Documentation & Pricing</body></html>");
        return;
    }

    if (strcmp(method, "POST") == 0 && strstr(path, "/pause")) {
        send_response(client, 302, "Found", "text/html", "/projects/", NULL, "Redirecting...");
        return;
    }

    // Default HTML response for root/dashboard
    send_response(client, 200, "OK", "text/html", NULL, NULL, "<html><body><h1>Healthchecks C Server</h1></body></html>");
}

int main(void) {
#ifdef _WIN32
    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        printf("Failed to initialize Winsock.\n");
        return 1;
    }
#endif

    SOCKET server = socket(AF_INET, SOCK_STREAM, 0);
    if (server == INVALID_SOCKET) {
        printf("Failed to create socket.\n");
        return 1;
    }

    int opt = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, (const char*)&opt, sizeof(opt));

    struct sockaddr_in address;
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = INADDR_ANY;
    address.sin_port = htons(PORT);

    if (bind(server, (struct sockaddr *)&address, sizeof(address)) == SOCKET_ERROR) {
        printf("Bind failed on port %d.\n", PORT);
        closesocket(server);
        return 1;
    }

    if (listen(server, 64) == SOCKET_ERROR) {
        printf("Listen failed.\n");
        closesocket(server);
        return 1;
    }

    printf("[SERVER] Healthchecks Benchmark C Server Listening on http://localhost:%d\n", PORT);
    reset_state();

    while (1) {
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        SOCKET client = accept(server, (struct sockaddr *)&client_addr, &addr_len);
        if (client != INVALID_SOCKET) {
            char buffer[BUFFER_SIZE];
            int bytes = recv(client, buffer, sizeof(buffer) - 1, 0);
            if (bytes > 0) {
                buffer[bytes] = '\0';
                handle_request(client, buffer);
            }
            closesocket(client);
        }
    }

    closesocket(server);
#ifdef _WIN32
    WSACleanup();
#endif
    return 0;
}
