from django.contrib.auth import get_user_model, password_validation
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("Username taken.")
        return value

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value


class TokenResponseSerializer(serializers.Serializer):
    token = serializers.CharField()


class RegisterView(APIView):
    """Create a user and return an API token (DRF TokenAuthentication)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(request=RegisterSerializer, responses={201: TokenResponseSerializer})
    def post(self, request):
        s = RegisterSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = User.objects.create_user(**s.validated_data)
        token = Token.objects.create(user=user)
        return Response({"token": token.key}, status=status.HTTP_201_CREATED)
