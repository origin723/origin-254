from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from apps.accounts.models import Role, User, VerificationDocument
from apps.catalog.models import InventoryItem, Product, ProductVariety
from apps.core.models import Location, MatchingConfig
from apps.orders.models import Delivery, Inspection
from apps.sourcing.models import SourcingRequest


def grouped_locations(field):
    """Show location dropdowns grouped by county (47 counties, 290 constituencies)."""
    groups = {}
    for loc in field.queryset.order_by("county", "sub_county", "name"):
        label = loc.name if loc.name != f"{loc.county} County" else f"{loc.county} County (whole county)"
        groups.setdefault(loc.county, []).append((loc.pk, label))
    field.choices = [("", "---------")] + [(county, opts) for county, opts in groups.items()]


class DateInput(forms.DateInput):
    """HTML date pickers only accept ISO dates (YYYY-MM-DD); other formats show as blank."""
    input_type = "date"

    def __init__(self, attrs=None, format="%Y-%m-%d"):
        super().__init__(attrs=attrs, format=format)


class RegisterForm(forms.Form):
    role = forms.ChoiceField(choices=[c for c in Role.choices if c[0] != Role.ADMIN])
    business_name = forms.CharField(max_length=160, label="Business / farm name")
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    username = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone = forms.CharField(max_length=16, help_text="e.g. +254712345678")
    location = forms.ModelChoiceField(queryset=Location.objects.all(), help_text="Your farm, store or factory")
    password = forms.CharField(widget=forms.PasswordInput, min_length=8)

    def clean_username(self):
        v = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=v).exists():
            raise forms.ValidationError("This username is taken.")
        return v

    def clean_email(self):
        v = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=v).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return v

    def clean(self):
        data = super().clean()
        if data.get("password"):
            validate_password(data["password"], User(username=data.get("username", ""), email=data.get("email", "")))
        return data

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        grouped_locations(self.fields["location"])


class NearestSuppliersForm(forms.Form):
    product = forms.CharField(max_length=120, label="Product", help_text="Type any product or pick one from the list")
    location = forms.ModelChoiceField(queryset=Location.objects.all(), label="Deliver to / your location")
    min_quantity = forms.DecimalField(required=False, min_value=0, max_digits=12, decimal_places=2,
                                      label="Minimum stock (optional)")
    radius_km = forms.IntegerField(required=False, min_value=5, max_value=1500, label="Search radius, km (optional)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].widget.attrs.update({"autocomplete": "off", "placeholder": "Type or pick, e.g. Mango",
                                                    "list": "product-options"})
        grouped_locations(self.fields["location"])

    def clean_product(self):
        from apps.catalog.services import clean_name, find_product
        name = clean_name(self.cleaned_data["product"])
        if len(name) < 2:
            raise forms.ValidationError("Type the product you need.")
        self.product_name = name
        return find_product(name)  # None when no supplier has listed it yet


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ("name", "county", "sub_county", "latitude", "longitude")


class SourcingRequestForm(forms.ModelForm):
    """Buyers type the product (and optionally the variety) they need: no pick-lists."""

    product_name = forms.CharField(max_length=120, label="Product",
                                   help_text="Type any product, e.g. Mango, Pineapple, Sorghum, Cashew nuts")
    variety_name = forms.CharField(max_length=80, required=False, label="Variety (optional)",
                                   help_text="Leave empty to accept any variety")

    class Meta:
        model = SourcingRequest
        fields = ("quantity", "unit", "delivery_location", "delivery_deadline",
                  "min_quality_grade", "quality_notes", "intended_application", "packaging_requirements",
                  "target_budget", "delivery_arrangement")
        widgets = {"delivery_deadline": DateInput(),
                   "quality_notes": forms.Textarea(attrs={"rows": 2, "placeholder": "if applicable"})}
        labels = {"delivery_location": "Preferred delivery location",
                  "target_budget": "Target budget, KES (optional)", "min_quality_grade": "Minimum quality grade",
                  "quality_notes": "Any other requirements"}

    field_order = ["product_name", "variety_name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        for name, options in (("product_name", "product-options"), ("variety_name", "variety-options")):
            self.fields[name].widget.attrs.update({"autocomplete": "off", "list": options})
        self.fields["product_name"].widget.attrs["placeholder"] = "Type or pick, e.g. Mango"
        self.fields["variety_name"].widget.attrs["placeholder"] = "e.g. Apple"
        self.fields["delivery_deadline"].initial = today + timedelta(days=7)
        self.fields["delivery_deadline"].widget.attrs["min"] = today.isoformat()
        self.fields["delivery_deadline"].help_text = "Allow time for transport: most suppliers need 1–3 days."
        self.fields["quantity"].widget.attrs.update({"min": "0.01", "step": "any"})
        self.fields["target_budget"].widget.attrs.update({"min": "1", "step": "any"})
        self.fields["target_budget"].error_messages["min_value"] = "The budget must be at least KES 1, or leave it empty."
        self.fields["unit"].help_text = "Crates and pieces only match suppliers who list in the same unit."
        grouped_locations(self.fields["delivery_location"])

    def clean_product_name(self):
        from apps.catalog.services import clean_name, find_product
        name = clean_name(self.cleaned_data["product_name"])
        if len(name) < 2:
            raise forms.ValidationError("Type the product you need.")
        existing = find_product(name)
        if existing and not existing.is_active:
            raise forms.ValidationError(f"{existing.name} is not currently traded on Origin 254.")
        return name

    def clean_variety_name(self):
        from apps.catalog.services import clean_name
        return clean_name(self.cleaned_data.get("variety_name"))

    def clean(self):
        data = super().clean()
        if data.get("delivery_deadline") and data["delivery_deadline"] < timezone.localdate():
            self.add_error("delivery_deadline", "The deadline cannot be in the past.")
        return data

    def request_data(self, user):
        """Cleaned data for create_sourcing_request, with typed names turned into catalogue entries."""
        from apps.catalog.services import resolve_product, resolve_variety
        data = dict(self.cleaned_data)
        product, self.product_created = resolve_product(data.pop("product_name"), user)
        data["product"] = product
        data["variety"] = resolve_variety(product, data.pop("variety_name"))
        return data


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        from apps.catalog.models import PHOTO_EXTENSIONS, validate_image_file
        single = super().clean
        files = [single(d, initial) for d in data if d] if isinstance(data, (list, tuple)) else (
            [single(data, initial)] if data else [])
        if self.required and not files:
            raise forms.ValidationError("Choose at least one photo.")
        for f in files:
            if f.name.rsplit(".", 1)[-1].lower() not in PHOTO_EXTENSIONS:
                raise forms.ValidationError(f"{f.name}: upload a JPG, PNG or WebP photo.")
            try:
                validate_image_file(f)
            except forms.ValidationError as exc:
                raise forms.ValidationError(f"{f.name}: {' '.join(exc.messages)}")
        return files


class PhotoUploadForm(forms.Form):
    photos = MultipleFileField(label="Product photos",
                               help_text=f"JPG, PNG or WebP, up to 5 MB each, {settings.MAX_PHOTOS_PER_LISTING} per listing")
    caption = forms.CharField(max_length=120, required=False, label="Caption (optional)")


class InventoryForm(forms.ModelForm):
    """Suppliers type the product and variety names themselves (no pick-lists)."""

    product_name = forms.CharField(max_length=100, label="Product name", help_text="e.g. Mango, Tomato, Maize")
    variety_name = forms.CharField(max_length=80, required=False, label="Variety (optional)",
                                   help_text="e.g. Apple, Ngowe, Kent")

    class Meta:
        model = InventoryItem
        fields = ("quantity_available", "unit", "quality_grade", "price_per_unit",
                  "location", "harvest_date", "available_from", "available_until", "can_deliver", "packaging")
        widgets = {"harvest_date": DateInput(), "available_from": DateInput(), "available_until": DateInput()}
        labels = {"price_per_unit": "Indicative price per unit (KES)"}

    other_details = forms.CharField(required=False, max_length=500, label="Any other details",
                                    widget=forms.Textarea(attrs={"rows": 2, "placeholder": "if applicable"}))
    photos = MultipleFileField(required=False, label="Product photos (at least 2)",
                               help_text=f"At least {settings.MIN_PHOTOS_PER_LISTING} clear photos of the actual produce. "
                                         f"JPG, PNG or WebP, up to 5 MB each, {settings.MAX_PHOTOS_PER_LISTING} per listing")

    field_order = ["product_name", "variety_name"]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for name, options in (("product_name", "product-options"), ("variety_name", "variety-options")):
            self.fields[name].widget.attrs.update({"autocomplete": "off", "list": options})
        grouped_locations(self.fields["location"])
        if self.instance.pk:
            self.fields["product_name"].initial = self.instance.product.name
            self.fields["variety_name"].initial = self.instance.variety.name if self.instance.variety else ""
        attrs = (self.instance.quality_attributes or {}) if self.instance.pk else {}
        self.fields["other_details"].initial = attrs.get("other_details", "")
        from django.conf import settings
        self.min_photos = settings.MIN_PHOTOS_PER_LISTING
        self.existing_photos = self.instance.photos.count() if self.instance.pk else 0
        if self.existing_photos < self.min_photos:
            self.fields["photos"].widget.attrs["required"] = "required"
        else:
            self.fields["photos"].label = "Add more photos (optional)"

    def clean_photos(self):
        from apps.catalog.models import MAX_PHOTOS_PER_LISTING
        photos = self.cleaned_data.get("photos") or []
        total = self.existing_photos + len(photos)
        if total < self.min_photos:
            need = self.min_photos - self.existing_photos
            raise forms.ValidationError(
                f"Every product needs at least {self.min_photos} photos. Please add {need} "
                f"{'more ' if self.existing_photos else ''}photo{'s' if need > 1 else ''}.")
        if total > MAX_PHOTOS_PER_LISTING:
            raise forms.ValidationError(f"Each listing can have up to {MAX_PHOTOS_PER_LISTING} photos.")
        return photos

    def clean_product_name(self):
        from apps.catalog.services import clean_name
        name = clean_name(self.cleaned_data["product_name"])
        if len(name) < 2:
            raise forms.ValidationError("Enter the product name.")
        existing = Product.objects.filter(name__iexact=name).first()
        if existing and not existing.is_active:
            raise forms.ValidationError(f"{existing.name} is not currently traded on Origin 254.")
        return name

    def clean(self):
        data = super().clean()
        if data.get("available_until") and data.get("available_from") and \
                data["available_until"] < data["available_from"]:
            self.add_error("available_until", "Must be after the available-from date.")
        other = (data.get("other_details") or "").strip()
        self.instance.quality_attributes = {"other_details": other} if other else {}
        return data

    def save(self, commit=True):
        from apps.catalog.services import resolve_product, resolve_variety
        product, self.product_created = resolve_product(self.cleaned_data["product_name"], self.user)
        self.instance.product = product
        self.instance.variety = resolve_variety(product, self.cleaned_data.get("variety_name"))
        return super().save(commit=commit)


class QuotationForm(forms.Form):
    inventory_item = forms.ModelChoiceField(queryset=InventoryItem.objects.none(), label="From listing")
    quantity = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    price_per_unit = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, label="Price per unit (KES)")
    delivery_cost = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, initial=0,
                                       label="Delivery cost (KES)")
    includes_delivery = forms.BooleanField(required=False, initial=True, label="I will deliver")
    earliest_delivery_date = forms.DateField(widget=DateInput())
    valid_until = forms.DateField(widget=DateInput())
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, supplier=None, request_obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        if supplier and request_obj:
            self.fields["inventory_item"].queryset = supplier.inventory.filter(product=request_obj.product,
                                                                               is_active=True)
            self.fields["earliest_delivery_date"].initial = min(timezone.localdate() + timedelta(days=2),
                                                                request_obj.delivery_deadline)
            self.fields["valid_until"].initial = timezone.localdate() + timedelta(days=5)


class CounterForm(forms.Form):
    price_per_unit = forms.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=0,
                                        label="Proposed price/unit")
    quantity = forms.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=0.01,
                                  label="Proposed quantity")
    note = forms.CharField(required=False)


class InspectionForm(forms.Form):
    stage = forms.ChoiceField(choices=Inspection.Stage.choices)
    result = forms.ChoiceField(choices=Inspection.Result.choices)
    grade_observed = forms.ChoiceField(choices=[("", "—")] + list(InventoryItem._meta.get_field(
        "quality_grade").choices), required=False)
    accepted_quantity = forms.DecimalField(required=False, max_digits=12, decimal_places=2)
    notes = forms.CharField(required=False, label="Any other observations", widget=forms.Textarea(attrs={"rows": 2, "placeholder": "if applicable"}))
    report = forms.FileField(required=False, help_text="PDF or photo")


class DeliveryUpdateForm(forms.Form):
    status = forms.ChoiceField(choices=Delivery.Status.choices)
    vehicle_reg = forms.CharField(required=False, max_length=20)
    driver_name = forms.CharField(required=False, max_length=80)
    received_quantity = forms.DecimalField(required=False, max_digits=12, decimal_places=2)
    received_by = forms.CharField(required=False, max_length=120)
    note = forms.CharField(required=False)
    proof_of_delivery = forms.FileField(required=False)


class ReviewForm(forms.Form):
    rating = forms.TypedChoiceField(choices=[(i, f"{i} ★") for i in range(5, 0, -1)], coerce=int)
    quality_rating = forms.TypedChoiceField(choices=[("", "—")] + [(i, i) for i in range(5, 0, -1)], coerce=int,
                                            required=False, empty_value=None)
    timeliness_rating = forms.TypedChoiceField(choices=[("", "—")] + [(i, i) for i in range(5, 0, -1)],
                                               coerce=int, required=False, empty_value=None)
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class DocumentForm(forms.ModelForm):
    class Meta:
        model = VerificationDocument
        fields = ("doc_type", "file")


class MatchingConfigForm(forms.ModelForm):
    class Meta:
        model = MatchingConfig
        exclude = ("updated_by",)
