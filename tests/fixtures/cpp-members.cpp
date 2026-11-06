// C++ members the struct parser must step over: access labels, inline
// methods, a constructor with an initializer list, static members, default
// and brace initializers. Vec stays exact; Derived inherits a base, so the
// parser cannot know its leading fields and checks order and widths only.
#include <cstdint>

// [x:4B][y:4B][z:4B][pad:4B]
struct alignas(16) Vec {
public:
    float x{0}, y{0};
    float z = 0;
    Vec() : x(0), y(0), z(0) {}
    float dot(const Vec& o) const { return x * o.x + y * o.y + z * o.z; }
    static const int kDims = 3;
private:
    int scratch() const;
};
static_assert(sizeof(Vec) == 16, "diagram: three floats padded to 16");

// [base:8B][extra:4B]: the base class fields come first.
struct Derived : public Base {
    uint32_t extra;
};
static_assert(sizeof(Derived) == 12, "diagram: base, then extra");
