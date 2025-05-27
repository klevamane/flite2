import decimal
import uuid
import secrets

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.conf import settings
from django.dispatch import receiver
from django.contrib.auth.models import AbstractUser
from django.utils.encoding import python_2_unicode_compatible
from django.db.models.signals import post_save
from rest_framework.authtoken.models import Token
from flite.core.models import BaseModel
from phonenumber_field.modelfields import PhoneNumberField
from django.utils import timezone
from model_utils.managers import InheritanceManager

from flite.core.utils import FAILURE_MSGS


@python_2_unicode_compatible
class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    def __str__(self):
        return self.username


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_auth_token(sender, instance=None, created=False, **kwargs):
    if created:
        Token.objects.create(user=instance)
        UserProfile.objects.create(user=instance)
        Balance.objects.create(owner=instance)


class Phonenumber(BaseModel):
    number = models.CharField(max_length=24)
    is_verified = models.BooleanField(default=False)
    is_primary = models.BooleanField(default=False)
    owner_email = models.EmailField()

    class Meta:
        verbose_name = "Phone Number"


class UserProfile(BaseModel):
    referral_code = models.CharField(max_length=120)
    user = models.OneToOneField('users.User',on_delete=models.CASCADE)


    def save(self, *args, **kwargs):
        if not self.referral_code:
            self.referral_code = self.generate_new_referal_code()
        return super(UserProfile, self).save(*args, **kwargs)


    def generate_new_referal_code(self):
        """
        Returns a unique passcode
        """
        def _passcode():
            return str(uuid.uuid4().hex)[0:8]
        passcode = _passcode()
        while UserProfile.objects.filter(referral_code=passcode).exists():
            passcode = _passcode()
        return passcode



class NewUserPhoneVerification(BaseModel):

    phone_number = PhoneNumberField(unique=True, blank=True, null=True)
    verification_code = models.CharField(max_length=30)
    is_verified = models.BooleanField(default=False)
    email = models.CharField(max_length=100)

    def __str__(self):
        return str(self.phone_number)+'-'+ str(self.verification_code)

    class Meta:
        verbose_name_plural = "New User Verification Codes"


class Referral(BaseModel):
    owner = models.OneToOneField('users.User',on_delete=models.CASCADE,related_name="owner")
    referred = models.OneToOneField('users.User',on_delete=models.CASCADE, related_name="referred")

    class Meta:
        verbose_name = "User referral"


class Balance(BaseModel):

    owner = models.OneToOneField(User, on_delete=models.CASCADE)
    book_balance = models.DecimalField(default=0.0, decimal_places=2, max_digits=9)
    available_balance = models.DecimalField(default=0.0, decimal_places=2, max_digits=9)
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Balance"
        verbose_name_plural = "Balances"

    def make_deposit(self, amount):
        self.available_balance += decimal.Decimal(amount)
        self.book_balance = self.available_balance
        self.save()
        self._make_transaction(
            amount, Deposit, status="complete", new_balance=self.available_balance
        )

    def make_withdrawal(self, amount):
        self._can_debit_or_error(amount)
        self.available_balance -= decimal.Decimal(amount)
        self.book_balance = self.available_balance
        self.save()
        self._make_transaction(
            amount, Withdrawal, status="complete", new_balance=self.available_balance
        )

    def _make_transaction(self, amount, klass, **kwargs):
        """
        Creates a transaction with information of
        passed as attributes passed

        Args:
            amount(number): The transaction amount
            klass(class): A model class
            kwargs(dict): Keyword arguments
        """
        tnx = klass(
            owner=self.owner,
            amount=amount,
            reference=make_refernce(klass.__name__.capitalize()),
            **kwargs
        )
        tnx.save()

    def _can_debit_or_error(self, amount):
        if self.available_balance < amount:
            raise ValidationError(FAILURE_MSGS["insufficient_funds"])

    def make_p2p_transfer(self, amount, target):
        self._can_debit_or_error(amount)
        with transaction.atomic():
            # lock for update
            self.get_lock()
            target.get_lock()

            self.available_balance -= amount
            target.available_balance += amount
            self.book_balance -= amount
            target.book_balance += amount
            self.save()
            target.save()

            self._make_transaction(
                amount,
                P2PTransfer,
                sender=self.owner,
                receipient=target.owner,
                status="complete",
                new_balance=self.available_balance,
            )

    def get_lock(self):
        Balance.objects.select_for_update().get(id=self.id)


def make_refernce(pre, length=11):
    """Generate a random reference with a starting subject pre"""
    return "{}{}".format(pre, secrets.token_hex(length))


class AllBanks(BaseModel):

    name = models.CharField(max_length=100)
    acronym = models.CharField(max_length=50)
    bank_code = models.CharField(max_length=50)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "All Banks"


class Bank(models.Model):

    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    bank = models.ForeignKey(AllBanks, on_delete=models.CASCADE)
    account_name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=50)
    account_type = models.CharField(max_length=50)

class Transaction(BaseModel):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transaction')
    reference = models.CharField(max_length=200)
    status = models.CharField(max_length=200)
    amount = models.DecimalField(default=0.0, decimal_places=2, max_digits=9)
    new_balance = models.DecimalField(default=0.0, decimal_places=2, max_digits=9)

    objects = InheritanceManager()


class Deposit(Transaction):
    pass


class Withdrawal(Transaction):
    pass

class BankTransfer(Transaction):
    bank = models.ForeignKey(Bank,on_delete=models.CASCADE)

    class Meta:
        verbose_name_plural = "Bank Transfers"


class P2PTransfer(Transaction):
    sender = models.ForeignKey(User, on_delete=models.CASCADE,related_name="sender")
    receipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recipient")

    class Meta:
        verbose_name_plural = "P2P Transfers"


class Card(models.Model):

    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    authorization_code = models.CharField(max_length=200)
    ctype = models.CharField(max_length=200)
    cbin = models.CharField(max_length=200, default=None)
    cbrand = models.CharField(max_length=200, default=None)
    country_code = models.CharField(max_length=200, default=None)
    first_name = models.CharField(max_length=200, default=None)
    last_name = models.CharField(max_length=200, default=None)
    number = models.CharField(max_length=200)
    bank = models.CharField(max_length=200)
    expiry_month = models.CharField(max_length=10)
    expiry_year = models.CharField(max_length=10)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    created_on = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.number

    def delete(self):
        self.is_active = False
        self.is_deleted = True
        self.save()



